"""One-shot, offline, one-GPU SGLang correctness scenarios; no resource APIs.

The controller owns the outer lease. This guest independently reserves 120s for
cleanup, records native groups before readiness, and never broadens tolerances.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

PROMPTS = ([4, 5, 6], [6, 5, 4], [4], [5], [6], [4, 4, 4], [7, 8, 9], [127, 126, 125])
TOLERANCE = 1e-3
MINIMUM_CHANGE = 0.01
POLICY_HEADER = "x-chalk-policy-version"
HEALTH_ROUTE = "/chalk/health"
METRICS_ROUTE = "/chalk/metrics"


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def identity(pid):
    try:
        # comm can contain spaces or parentheses; fields after its final ')' are stable.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return {
        "pid": pid,
        "start_ticks": fields[19],
        "session": int(fields[3]),
        "group": int(fields[2]),
        "state": fields[0],
    }


def group_members(known):
    actual = identity(known["pid"])
    if actual is not None and actual["start_ticks"] != known["start_ticks"]:
        raise RuntimeError("Native process identity changed; refuse unrelated signal")
    members = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            value = identity(int(path.name))
            if (
                value is not None
                and value["group"] == known["pid"]
                and value["state"] != "Z"
            ):
                if value["session"] != known["pid"] or int(value["start_ticks"]) < int(
                    known["start_ticks"]
                ):
                    raise RuntimeError("Native process group identity differs")
                members.append(value)
    return members


def signal_group(known, sig):
    if group_members(known):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(known["pid"], sig)


async def stop_group(known):
    signal_group(known, signal.SIGTERM)
    until = time.time() + 10
    while group_members(known) and time.time() < until:
        await asyncio.sleep(0.1)
    if group_members(known):
        signal_group(known, signal.SIGKILL)
        until = time.time() + 5
        while group_members(known) and time.time() < until:
            await asyncio.sleep(0.1)
    if group_members(known):
        raise RuntimeError("Native group members remain after bounded stop")


def managed_child(args):
    """Only test subprocesses install the fault; product code stays unchanged."""
    from chalkcompute.weights.serving import __main__ as entry
    from chalkcompute.weights.serving.adapters.sglang import SGLangAdapter

    original_gate = entry.AdmissionGate

    class ObservedGate(original_gate):
        @contextlib.asynccontextmanager
        async def update(self, timeout):
            event = {
                "version_before": self.version,
                "active_before_drain": self._active,
                "entered_at": time.time(),
            }
            async with super().update(timeout):
                event.update(active_after_drain=self._active, drained_at=time.time())
                log = args.output / "gate-updates.json"
                previous = json.loads(log.read_bytes()) if log.exists() else []
                write_json(log, previous + [event])
                yield

    entry.AdmissionGate = ObservedGate
    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*command, **kwargs):
        process = await original_spawn(*command, **kwargs)
        native = identity(process.pid)
        if native is None or native["session"] != process.pid:
            raise RuntimeError("Native session identity missing")
        write_json(args.output / "native-group.json", native)
        return process

    entry.asyncio.create_subprocess_exec = spawn

    class LostAckAdapter(SGLangAdapter):
        async def _post(self, path, body, *, json_result=True):
            response = await super()._post(path, body, json_result=json_result)
            if path == "/update_weights_from_disk" and response.get("success") is True:
                write_json(
                    args.output / "actual-native-reload-before-lost-ack.json", response
                )
                raise RuntimeError(
                    "Injected lost reload acknowledgement after actual native success"
                )
            return response

    if args.lost_ack:
        entry.SGLangAdapter = LostAckAdapter
    os.environ.update(
        CHALK_LISTEN_ADDR="127.0.0.1:8000",
        CHALK_WEIGHT_SYNC_POLL_S="0.1",
        CHALK_WEIGHT_SYNC_DRAIN_TIMEOUT_S="30",
    )
    asyncio.run(
        entry.serve(
            argparse.Namespace(
                engine="sglang-experimental",
                weights=str(args.stream),
                engine_port=8001,
                startup_timeout=args.startup_timeout,
                engine_args=[
                    "--context-length",
                    "8192",
                    "--mem-fraction-static",
                    "0.5",
                ],
            )
        )
    )


class Native:
    def __init__(self, root, deadline):
        self.root, self.deadline = root, deadline
        self.process = None
        self.log = None
        self.directory = None
        self.counter = 0
        self.checkpoint_baseline = set(Path("/tmp").glob("chalk-weight-checkpoint-*"))

    def budget(self, maximum):
        left = self.deadline - time.time() - 120
        if left <= 0:
            raise TimeoutError("GPU test reached cleanup reserve")
        return min(maximum, left)

    async def start(self, model, *, managed=True, lost_ack=False, cold=False):
        await self.stop()
        self.counter += 1
        self.directory = self.root / f"native-{self.counter:02d}"
        self.directory.mkdir()
        startup = self.budget(300 if cold else 180)
        if managed:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--managed",
                "--stream",
                str(model),
                "--output",
                str(self.directory),
                "--startup-timeout",
                str(startup),
            ]
            if lost_ack:
                command.append("--lost-ack")
            port = 8000
        else:
            from chalkcompute.weights.serving.adapters.sglang import SGLangAdapter

            command = SGLangAdapter(
                8001, ["--context-length", "8192", "--mem-fraction-static", "0.5"]
            ).launch_cmd(model, 8001)
            port = 8001
        self.log = (self.directory / "native.log").open("wb")
        self.process = subprocess.Popen(
            command, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True
        )
        write_json(self.directory / "driver-group.json", identity(self.process.pid))
        if not managed:
            write_json(self.directory / "native-group.json", identity(self.process.pid))
        health_path = HEALTH_ROUTE if managed else "/health"
        until = time.time() + startup
        last_progress, previous_bytes = time.time(), 0
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as client:
            while time.time() < until:
                current_bytes = (self.directory / "native.log").stat().st_size
                if current_bytes > previous_bytes:
                    last_progress, previous_bytes = time.time(), current_bytes
                if time.time() - last_progress > 180:
                    raise TimeoutError(
                        "Native startup made no log progress for180seconds"
                    )
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"Native exited during startup; see {self.directory.name}"
                    )
                try:
                    async with client.get(
                        f"http://127.0.0.1:{port}" + health_path
                    ) as response:
                        if response.status == 200:
                            return
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError("Native startup exceeded fixed phase budget")

    async def stop(self):
        if self.process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self.process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(self.process.wait), 20)
        except TimeoutError:
            signal_group(
                json.loads((self.directory / "driver-group.json").read_bytes()),
                signal.SIGKILL,
            )
            await asyncio.wait_for(asyncio.to_thread(self.process.wait), 10)
        registry = self.directory / "native-group.json"
        if registry.exists():
            known = json.loads(registry.read_bytes())
            await stop_group(known)
        leftovers = (
            set(Path("/tmp").glob("chalk-weight-checkpoint-*"))
            - self.checkpoint_baseline
        )
        if leftovers:
            raise RuntimeError("Managed checkpoint assets remain after joined shutdown")
        self.log.close()
        write_json(
            self.directory / "stopped.json",
            {"driver_exit_code": self.process.returncode, "checked_at": time.time()},
        )
        self.process = None


async def request(client, path, *, port=8000, body=None):
    method = client.post if body is not None else client.get
    async with method(
        f"http://127.0.0.1:{port}" + path,
        **({"json": body} if body is not None else {}),
    ) as response:
        response.raise_for_status()
        return await response.json(), dict(response.headers)


def completion(prompt, *, model, stream=False):
    return {
        "model": model,
        "prompt": prompt,
        "max_tokens": 2048 if stream else 8,
        "temperature": 0,
        "logprobs": 5,
        "ignore_eos": True,
        "stream": stream,
    }


async def samples(client, *, port=8000, version=None):
    models, _ = await request(client, "/v1/models", port=port)
    model = models["data"][0]["id"]
    result = []
    for prompt in PROMPTS:
        value, headers = await request(
            client,
            "/v1/completions",
            port=port,
            body=completion(list(prompt), model=model),
        )
        if version is not None and headers.get(POLICY_HEADER) != str(version):
            raise AssertionError("Inference policy header differs from applied version")
        choice = value["choices"][0]
        result.append({"text": choice["text"], "logprobs": choice["logprobs"]})
    return result


def compare(actual, expected):
    maximum = 0.0
    if len(actual) != len(expected):
        raise AssertionError("Oracle prompt count differs")
    for observed, reference in zip(actual, expected, strict=True):
        if observed["text"] != reference["text"]:
            raise AssertionError("Greedy native output differs from fresh oracle")
        a, b = observed["logprobs"], reference["logprobs"]
        if a["tokens"] != b["tokens"] or len(a["top_logprobs"]) != len(
            b["top_logprobs"]
        ):
            raise AssertionError("Native token sequence differs from oracle")
        for left, right in zip(a["top_logprobs"], b["top_logprobs"], strict=True):
            if left.keys() != right.keys():
                raise AssertionError("Native top-five token set differs from oracle")
            for key in left:
                delta = abs(left[key] - right[key])
                if not math.isfinite(delta) or delta > TOLERANCE:
                    raise AssertionError(
                        "Native top-five logprob differs beyond fixed tolerance"
                    )
                maximum = max(maximum, delta)
    return maximum


def measurable_change(previous, current):
    maximum, token_change = 0.0, False
    for left, right in zip(previous, current, strict=True):
        a, b = left["logprobs"], right["logprobs"]
        if left["text"] != right["text"] or a["tokens"] != b["tokens"]:
            token_change = True
        for x, y in zip(a["top_logprobs"], b["top_logprobs"], strict=True):
            for key in x.keys() & y.keys():
                maximum = max(maximum, abs(x[key] - y[key]))
    if not math.isfinite(maximum) or maximum <= MINIMUM_CHANGE:
        raise AssertionError(
            "Consecutive native fixture versions lack fixed measurable change"
        )
    return {"token_change": token_change, "maximum_common_logprob_change": maximum}


def pointer(root, name, version):
    from chalkcompute._weights_format import pointer_json

    temp = root / (name + ".tmp")
    temp.write_bytes(pointer_json(version))
    temp.replace(root / name)


async def wait_version(client, native, version, *, port=8000):
    until = time.time() + native.budget(30)
    while time.time() < until:
        value, _ = await request(client, HEALTH_ROUTE, port=port)
        if value == {"applied_version": version, "healthy": True}:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("Update exceeded fixed 30-second budget")


async def run(args):
    import aiohttp

    args.output.mkdir(parents=True, exist_ok=False)
    stream = args.output / "stream"
    shutil.copytree(args.fixture / "stream", stream)
    native = Native(args.output, args.deadline)
    proof = {
        "status": "running",
        "scenarios": {},
        "tolerance": TOLERANCE,
        "minimum_change": MINIMUM_CHANGE,
        "scope": "native supervisor correctness; immutable fixture copied locally; CURRENT/PIN changes guest-local",
        "prompts": PROMPTS,
    }

    def mark(name, result):
        proof["scenarios"][name] = result
        write_json(args.output / "gpu-proof.json", proof)

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            await native.start(stream, cold=True)
            captured = {0: await samples(client, version=0)}
            models, _ = await request(client, "/v1/models")
            model = models["data"][0]["id"]
            mark("cold_v0", "passed")
            # Hold a real admitted stream while publishing. Its first response
            # header binds v0; final data is consumed before v1 is accepted.
            async with client.post(
                "http://127.0.0.1:8000/v1/completions",
                json=completion([4, 5, 6], model=model, stream=True),
            ) as response:
                response.raise_for_status()
                if response.headers.get(POLICY_HEADER) != "0":
                    raise AssertionError("Drain stream did not bind old policy")
                first = await response.content.readline()
                if not first:
                    raise AssertionError("Native stream returned no data")
                pointer(stream, "CURRENT", 1)
                body = await response.read()
                if b"[DONE]" not in first + body:
                    raise AssertionError("Drain stream did not complete")
            await wait_version(client, native, 1)
            captured[1] = await samples(client, version=1)
            observed = json.loads(
                (native.directory / "gate-updates.json").read_bytes()
            )[-1]
            if (
                observed["active_before_drain"] < 1
                or observed["active_after_drain"] != 0
            ):
                raise AssertionError(
                    "Update did not overlap and drain an admitted stream"
                )
            mark("stream_drain_update_v1", observed)
            # Disconnect an actual native stream, then require drain/update to
            # complete without exposing a version before the native ACK.
            response = await client.post(
                "http://127.0.0.1:8000/v1/completions",
                json=completion([6, 5, 4], model=model, stream=True),
            )
            response.raise_for_status()
            await response.content.readline()
            response.close()
            pointer(stream, "CURRENT", 2)
            await wait_version(client, native, 2)
            captured[2] = await samples(client, version=2)
            observed = json.loads(
                (native.directory / "gate-updates.json").read_bytes()
            )[-1]
            if (
                observed["active_before_drain"] < 1
                or observed["active_after_drain"] != 0
            ):
                raise AssertionError(
                    "Disconnected native request was not drained before update"
                )
            mark("disconnect_drain_update_v2", observed)
            mark(
                "measurable_changes",
                [
                    measurable_change(captured[0], captured[1]),
                    measurable_change(captured[1], captured[2]),
                ],
            )
            # Pointer replay/no-op must not call native reload again.
            metrics_before = await client.get("http://127.0.0.1:8000" + METRICS_ROUTE)
            before = await metrics_before.text()
            pointer(stream, "CURRENT", 2)
            await asyncio.sleep(0.4)
            metrics_after = await client.get("http://127.0.0.1:8000" + METRICS_ROUTE)
            after = await metrics_after.text()

            def updates(text):
                return next(
                    line
                    for line in text.splitlines()
                    if line.startswith("chalk_weight_sync_updates ")
                )

            if updates(before) != updates(after):
                raise AssertionError("No-op target reloaded native model")
            mark("noop_v2", "passed")
            pointer(stream, "PIN", 0)
            await wait_version(client, native, 0)
            compare(await samples(client, version=0), captured[0])
            mark("pin_rollback_v0", "passed")
            # Corrupt one existing payload; keep old native policy healthy.
            async with client.get("http://127.0.0.1:8000" + METRICS_ROUTE) as response:
                prior_metrics = await response.text()
            failures_before_corruption = float(
                next(
                    line.split()[1]
                    for line in prior_metrics.splitlines()
                    if line.startswith("chalk_weight_sync_failures ")
                )
            )
            manifest = json.loads((stream / "v000001" / "manifest.json").read_bytes())
            patch = stream / "v000001" / manifest["tensors"][0]["file"]
            original = patch.read_bytes()
            patch.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            (stream / "PIN").unlink()
            pointer(stream, "CURRENT", 1)
            until = time.time() + native.budget(10)
            while time.time() < until:
                async with client.get(
                    "http://127.0.0.1:8000" + METRICS_ROUTE
                ) as response:
                    text = await response.text()
                failures = float(
                    next(
                        line.split()[1]
                        for line in text.splitlines()
                        if line.startswith("chalk_weight_sync_failures ")
                    )
                )
                if failures > failures_before_corruption:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(
                    "Corrupt fixture did not reach verification failure"
                )
            await wait_version(client, native, 0)
            compare(await samples(client, version=0), captured[0])
            mark("corrupt_payload_before_mutation", "passed")
            pointer(stream, "CURRENT", 0)
            patch.write_bytes(original)
            await native.stop()
            # Actual successful native disk mutation, then discard its ACK in
            # this test subclass. Supervisor must terminate the native group.
            await native.start(stream, lost_ack=True)
            pointer(stream, "CURRENT", 1)
            until = time.time() + native.budget(30)
            while native.process.poll() is None and time.time() < until:
                await asyncio.sleep(0.1)
            if native.process.poll() is None or native.process.returncode == 0:
                raise AssertionError("Lost reload ACK did not fail closed")
            marker = native.directory / "actual-native-reload-before-lost-ack.json"
            if not marker.exists():
                raise AssertionError("Lost ACK did not follow actual native mutation")
            await native.stop()
            mark("lost_reload_ack_after_native_mutation", "passed")
            await native.start(stream)
            compare(await samples(client, version=1), captured[1])
            mark("restart_after_lost_ack", "passed")
            await native.stop()
            for version in (0, 1, 2):
                await native.start(args.fixture / f"oracle-v{version}", managed=False)
                reference = await samples(client, port=8001)
                error = compare(captured[version], reference)
                mark(
                    f"fresh_native_oracle_v{version}", {"maximum_logprob_error": error}
                )
                await native.stop()
        proof["status"] = "passed"
    except BaseException as error:
        proof.update(
            status="failed", error_type=type(error).__name__, error=str(error)[:2048]
        )
        raise
    finally:
        await native.stop()
        proof["native_groups_stopped"] = True
        proof["finished_at"] = time.time()
        write_json(args.output / "gpu-proof.json", proof)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--lost-ack", action="store_true")
    parser.add_argument("--stream", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--startup-timeout", type=float, default=180)
    arguments = parser.parse_args()
    if arguments.managed:
        managed_child(arguments)
    else:
        asyncio.run(run(arguments))
