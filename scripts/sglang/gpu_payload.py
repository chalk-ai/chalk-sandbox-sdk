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
import tarfile
import hashlib
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


def bundle_diagnostics(out):
    paths = []
    for pattern in (
        "samples/**/*.json",
        "comparisons/**/*.json",
        "cache-state-*.json",
        "native-flush-ack.json",
        "native-*/*.json",
        "native-*/native.log",
    ):
        paths.extend(out.glob(pattern))
    paths = sorted(set(path for path in paths if path.is_file()))
    if (
        any(path.is_symlink() for path in paths)
        or sum(path.stat().st_size for path in paths) > 4 * 1024 * 1024
    ):
        raise ValueError("Native diagnostics exceed4MiB ordinary-file bound")
    target = out / "diagnostics.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(out), recursive=False)
    data = target.read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Native diagnostic bundle exceeds2MiB export bound")
    return {
        "file": target.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def meaningful_log_lines(text):
    return [
        line
        for line in text.splitlines()
        if line.strip()
        and not any(route in line for route in ("GET /health ", "GET /chalk/health "))
    ]


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
        last_progress, previous_message = time.time(), None
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as client:
            while time.time() < until:
                with (self.directory / "native.log").open("rb") as handle:
                    handle.seek(
                        max(0, (self.directory / "native.log").stat().st_size - 8192)
                    )
                    meaningful = meaningful_log_lines(
                        handle.read(8192).decode(errors="replace")
                    )
                if meaningful and meaningful[-1] != previous_message:
                    last_progress, previous_message = time.time(), meaningful[-1]
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


async def samples(client, *, port=8000, version=None, out=None, label="samples"):
    models, _ = await request(client, "/v1/models", port=port)
    model = models["data"][0]["id"]
    result = []
    if out is not None:
        (out / "samples" / label).mkdir(parents=True, exist_ok=False)
    for prompt_index, prompt in enumerate(PROMPTS):
        value, headers = await request(
            client,
            "/v1/completions",
            port=port,
            body=completion(list(prompt), model=model),
        )
        if version is not None and headers.get(POLICY_HEADER) != str(version):
            raise AssertionError("Inference policy header differs from applied version")
        if out is not None:
            write_json(
                out / "samples" / label / f"prompt-{prompt_index:02d}.json",
                {
                    "prompt": prompt,
                    "response": value,
                    "policy_header": headers.get(POLICY_HEADER),
                    "observed_at": time.time(),
                },
            )
        choice = value["choices"][0]
        result.append({"text": choice["text"], "logprobs": choice["logprobs"]})
    return result


class NativeComparisonError(AssertionError):
    def __init__(self, message, details):
        self.details = details
        super().__init__(message + "; " + json.dumps(details, sort_keys=True))


def compare(actual, expected, *, out=None, label="comparison"):
    """Scan all values and save per-prompt evidence before rejecting a result."""
    maximum, location, errors = 0.0, None, []
    directory = out / "comparisons" / label if out is not None else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=False)
    if len(actual) != len(expected):
        errors.append("Oracle prompt count differs")
    for prompt_index, (observed, reference) in enumerate(zip(actual, expected)):
        if directory is not None:
            write_json(
                directory / f"prompt-{prompt_index:02d}.json",
                {
                    "prompt": PROMPTS[prompt_index],
                    "observed": observed,
                    "reference": reference,
                },
            )
        if observed["text"] != reference["text"]:
            errors.append("Greedy native output differs from fresh oracle")
        left, right = observed["logprobs"], reference["logprobs"]
        if left["tokens"] != right["tokens"] or len(left["top_logprobs"]) != len(
            right["top_logprobs"]
        ):
            errors.append("Native token sequence differs from oracle")
        for token_index, (x, y) in enumerate(
            zip(left["top_logprobs"], right["top_logprobs"])
        ):
            if x.keys() != y.keys():
                errors.append("Native top-five token set differs from oracle")
            for token in x.keys() & y.keys():
                delta = abs(x[token] - y[token])
                if not math.isfinite(delta):
                    errors.append("Native top-five logprob is nonfinite")
                elif delta > maximum:
                    maximum = delta
                    location = {
                        "prompt_index": prompt_index,
                        "generated_token_index": token_index,
                        "token": token,
                        "observed": x[token],
                        "reference": y[token],
                        "absolute_error": delta,
                    }
    if maximum > TOLERANCE:
        errors.append("Native top-five logprob differs beyond fixed tolerance")
    details = {
        "maximum_logprob_error": maximum,
        "maximum_error_location": location,
        "tolerance": TOLERANCE,
        "errors": list(dict.fromkeys(errors)),
        "label": label,
    }
    if directory is not None:
        write_json(directory / "summary.json", details)
    if errors:
        raise NativeComparisonError(errors[0], details)
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
            captured = {
                0: await samples(client, version=0, out=args.output, label="cold-v0")
            }
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
            captured[1] = await samples(
                client, version=1, out=args.output, label="updated-v1"
            )
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
            captured[2] = await samples(
                client, version=2, out=args.output, label="updated-v2"
            )
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
            compare(
                await samples(client, version=0, out=args.output, label="rollback-v0"),
                captured[0],
                out=args.output,
                label="rollback-vs-cold-v0",
            )
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
            compare(
                await samples(
                    client, version=0, out=args.output, label="corruption-v0"
                ),
                captured[0],
                out=args.output,
                label="corruption-vs-cold-v0",
            )
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
            compare(
                await samples(client, version=1, out=args.output, label="restart-v1"),
                captured[1],
                out=args.output,
                label="restart-vs-updated-v1",
            )
            mark("restart_after_lost_ack", "passed")
            await native.stop()
            for version in (0, 1, 2):
                await native.start(args.fixture / f"oracle-v{version}", managed=False)
                reference = await samples(
                    client, port=8001, out=args.output, label=f"fresh-v{version}"
                )
                error = compare(
                    captured[version],
                    reference,
                    out=args.output,
                    label=f"updated-vs-fresh-v{version}",
                )
                mark(
                    f"fresh_native_oracle_v{version}", {"maximum_logprob_error": error}
                )
                await native.stop()
        proof["status"] = "passed"
    except BaseException as error:
        proof.update(
            status="failed", error_type=type(error).__name__, error=str(error)[:2048]
        )
        if isinstance(error, NativeComparisonError):
            proof["error_context"] = error.details
        raise
    finally:
        await native.stop()
        proof["native_groups_stopped"] = True
        proof["finished_at"] = time.time()
        proof["diagnostic_bundle"] = bundle_diagnostics(args.output)
        write_json(args.output / "gpu-proof.json", proof)


async def cache_state(client, out, label):
    server, _ = await request(client, "/server_info", port=8001)
    model, _ = await request(client, "/model_info", port=8001)
    allowed = {
        "disable_radix_cache",
        "weight_cache_mode",
        "disable_cuda_graph",
        "attention_backend",
        "sampling_backend",
        "dtype",
        "enable_deterministic_inference",
        "context_length",
        "mem_fraction_static",
        "cuda_graph_backend_decode",
        "cuda_graph_backend_prefill",
        "world_size",
        "effective_max_running_requests_per_dp",
        "last_gen_throughput",
    }

    def selected(value):
        result = {}
        if isinstance(value, dict):
            for key in sorted(allowed):
                if key in value:
                    item = value[key]
                    if isinstance(item, (str, int, float, bool, type(None))):
                        result[key] = bounded_scalar(item)
        return result

    def bounded_scalar(value):
        data = json.dumps(value).encode()
        if len(data) <= 128:
            return value
        return {"omitted_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    # Pinned SGLang internal_states repeats the full resolved server config,
    # plus topology/memory/debug/environment fields. Persist only the audited
    # cache-relevant scalars, and hash/count the complete response rather than
    # copying arbitrary configuration/debug state into the bounded artifact.
    raw = json.dumps(server, sort_keys=True, separators=(",", ":")).encode()
    internal = server.get("internal_states")
    internal_list = internal if isinstance(internal, list) else []
    state = {
        "observed_at": time.time(),
        "config": selected(server),
        "internal_states": [selected(item) for item in internal_list[:4]],
        "internal_state_count": len(internal_list),
        "internal_states_omitted": max(0, len(internal_list) - 4),
        "server_response_bytes": len(raw),
        "server_response_sha256": hashlib.sha256(raw).hexdigest(),
        "model": {
            key: bounded_scalar(model.get(key))
            for key in ("model_path", "load_format", "weight_version", "dtype")
            if isinstance(model.get(key), (str, int, float, bool, type(None)))
        },
    }
    if len(json.dumps(state).encode()) > 16384:
        raise ValueError("Native cache-state diagnostic exceeds16KiB bound")
    write_json(out / ("cache-state-" + label + ".json"), state)
    return {
        "file": "cache-state-" + label + ".json",
        "sha256": hashlib.sha256(
            (out / ("cache-state-" + label + ".json")).read_bytes()
        ).hexdigest(),
        "config": state["config"],
    }


async def run_cache_diagnostic(args):
    """Isolate cache-dependent drift with unchanged weights and fixed tolerance."""
    import aiohttp

    args.output.mkdir(parents=True, exist_ok=False)
    native = Native(args.output, args.deadline)
    proof = {
        "status": "running",
        "scope": "same native v0: warm-prefix repeat vs explicit flush cold vs fresh process; no weight updates",
        "tolerance": TOLERANCE,
        "comparisons": {},
        "scenarios": {},
    }

    def mark(label, value):
        proof["scenarios"][label] = value
        write_json(args.output / "gpu-proof.json", proof)

    def evaluate(label, observed, reference):
        try:
            maximum = compare(observed, reference, out=args.output, label=label)
            result = {"passed": True, "maximum_logprob_error": maximum}
        except NativeComparisonError as error:
            result = {"passed": False, **error.details}
        proof["comparisons"][label] = result
        write_json(args.output / "gpu-proof.json", proof)

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            await native.start(args.fixture / "oracle-v0", managed=False, cold=True)
            mark(
                "cold_native_ready",
                await cache_state(client, args.output, "cold-ready"),
            )
            cold = await samples(client, port=8001, out=args.output, label="cold-v0")
            mark("cold_v0_samples", "captured")
            for repeat in (1, 2):
                warm = await samples(
                    client, port=8001, out=args.output, label=f"warm-v0-{repeat}"
                )
                mark(
                    f"warm_v0_{repeat}",
                    await cache_state(client, args.output, f"warm-{repeat}"),
                )
                evaluate(f"warm-{repeat}-vs-cold", warm, cold)
            async with client.post(
                "http://127.0.0.1:8001/flush_cache", json={}
            ) as response:
                body = await response.text()
                acknowledgement = {
                    "status": response.status,
                    "body": body[:4096],
                    "acknowledged_at": time.time(),
                }
                write_json(args.output / "native-flush-ack.json", acknowledgement)
                if response.status != 200:
                    raise RuntimeError("Explicit native cache flush not acknowledged")
            mark("explicit_flush", acknowledgement)
            flushed = await samples(
                client, port=8001, out=args.output, label="flushed-cold-v0"
            )
            mark("flushed_cold_v0", await cache_state(client, args.output, "flushed"))
            evaluate("flushed-vs-cold", flushed, cold)
            await native.stop()
            await native.start(args.fixture / "oracle-v0", managed=False)
            fresh = await samples(
                client, port=8001, out=args.output, label="fresh-process-v0"
            )
            mark("fresh_process_v0", await cache_state(client, args.output, "fresh"))
            evaluate("fresh-process-vs-cold", fresh, cold)
        proof["diagnostic_complete"] = True
        proof["status"] = (
            "passed"
            if all(item["passed"] for item in proof["comparisons"].values())
            else "failed"
        )
        if proof["status"] != "passed":
            raise AssertionError(
                "Native cache diagnostic retained comparisons exceeding fixed1e-3"
            )
    except BaseException as error:
        proof.update(
            status="failed", error_type=type(error).__name__, error=str(error)[:2048]
        )
        raise
    finally:
        await native.stop()
        proof.update(native_groups_stopped=True, finished_at=time.time())
        proof["diagnostic_bundle"] = bundle_diagnostics(args.output)
        write_json(args.output / "gpu-proof.json", proof)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--cache-diagnostic", action="store_true")
    parser.add_argument("--lost-ack", action="store_true")
    parser.add_argument("--stream", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--startup-timeout", type=float, default=180)
    arguments = parser.parse_args()
    if arguments.managed:
        managed_child(arguments)
    elif arguments.cache_diagnostic:
        asyncio.run(run_cache_diagnostic(arguments))
    else:
        asyncio.run(run(arguments))
