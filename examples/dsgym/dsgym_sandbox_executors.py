#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.13.0", "fastapi>=0.110", "uvicorn>=0.29"]
# ///
"""Run DSGym's code execution on Chalk sandboxes instead of Docker.

[DSGym](https://github.com/fannie1208/DSGym) evaluates and trains data-science
agents. In every episode the model writes Python, DSGym runs it next to the
task's data files, and the output goes back to the model for its next turn.
Variables persist between turns, as in a notebook.

DSGym expects a "manager" HTTP service in front of a fleet of Docker
containers, each running a Jupyter kernel. You normally start that fleet
yourself with `docker compose`, which is not possible inside a Chalk container.
This script is a drop-in replacement for that manager. It serves the same HTTP
API, so DSGym needs no code changes. Point its `manager_url` at this server.
Behind it is a pool of Chalk sandboxes, one per concurrent episode.

## How it works

1. Build one image with the Python packages DSGym's executor installs, and
   reuse its URI for every sandbox so the pool triggers a single build.
2. Start `--pool-size` sandboxes. Each runs a small long-lived Python process,
   `KERNEL` below, that reads code on stdin and executes it in one namespace.
   That process is what makes state persist between turns; each separate
   `sandbox.exec` call would start a fresh interpreter.
3. `/allocate` hands an episode a ready sandbox. `/session/{id}/execute` sends
   the model's code to that sandbox's kernel. `/deallocate/{id}` restarts the
   kernel with an empty working directory and returns the sandbox to the pool.

A sandbox takes one to three minutes to start, so the pool is created once up
front and reused. Restarting a kernel between episodes takes under a second.
If a sandbox dies, the episode using it gets a `KernelDied` error for that
cell, as a notebook does when its kernel crashes, and the slot starts a
replacement sandbox.

## Safety

This executes model-generated code. The sandboxes are isolated, run without a
Chalk identity, and never see this process's credentials. `--no-network`
blocks their outbound traffic; leave it off only if tasks need the internet.
Task data is mounted from a Chalk volume pinned to one immutable version, so
code in one episode cannot change the files another episode reads.

## Resources and cost

- **Ephemeral:** the `--pool-size` sandboxes. They are terminated when the
  server exits, including on Ctrl-C, and `--sandbox-lifetime` bounds them if
  this process is killed first. A pool of 64 is 64 billable sandboxes.
- **Intentionally persistent:** the data volume, when `--upload-data` creates
  it. It keeps the task files for later runs; delete it when you are done.
- **Caller-owned:** a volume passed with `--data-volume` and no `--upload-data`
  is only read.
- **Platform-managed:** the built image is cached for reuse.

## Prerequisites

Chalk credentials in the standard environment variables, and DSGym's task
data: the contents of its `data/data` directory, which DSGym's own setup
mounts at `/data`. Task prompts refer to files by `/data/...` paths, so the
volume is mounted at the same place.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import chalkcompute
from chalkcompute import (
    Image,
    NetworkPolicy,
    Sandbox,
    SandboxNotFoundError,
    Volume,
    VolumeClient,
)

logger = logging.getLogger("dsgym-sandbox-executors")

# DSGym's configs call the standard executor type "executor-prebuilt". Its
# client allocates without naming a type, so this only matters to callers
# that pass `container_type`.
CONTAINER_TYPE = "executor-prebuilt"

DATA_MOUNT_PATH = "/data"
WORKDIR = "/workspace"

# The data-science stack from DSGym's executors/container_images/instance/
# requirements.txt. Its HTTP server and Jupyter packages are left out because
# this script replaces them. torch is installed separately from the CPU wheel
# index, since the sandboxes have no GPU.
EXECUTOR_PACKAGES = [
    "numpy",
    "scikit-learn",
    "pandas",
    "scipy",
    "requests",
    "sympy",
    "tavily-python",
    "statsmodels",
    "dowhy[causal-discovery]",
    "econml",
    "causalml",
    "linearmodels",
    "networkx",
    "pyarrow",
    "openpyxl",
    "xlrd",
    "pyreadstat",
    "tabulate",
    "lingam",
    "pgmpy",
]

# Environment every kernel inherits. WORKSPACE_FOLDER and the warning filter
# match DSGym's executor; Agg keeps matplotlib from looking for a display.
SANDBOX_ENV = {
    "WORKSPACE_FOLDER": DATA_MOUNT_PATH,
    "PYTHONWARNINGS": "ignore::FutureWarning",
    "MPLBACKEND": "Agg",
}

# The long-lived process in each sandbox. It reads one JSON request per line
# on stdin and writes one JSON reply per line, in the output format DSGym's
# Jupyter executor returns: "stream" items for stdout and stderr, a "result"
# item for the value of a trailing expression, and an "error" item with a
# traceback. It keeps the reply channel on a private copy of stdout and points
# file descriptor 1 at stderr, so output from subprocesses or C extensions
# cannot corrupt a reply.
KERNEL = r"""
import ast, io, json, os, signal, sys, traceback

class _Timeout(BaseException):
    pass

def _on_alarm(signum, frame):
    raise _Timeout()

signal.signal(signal.SIGALRM, _on_alarm)
replies = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
requests = sys.stdin
sys.stdin = io.StringIO()
namespace = {"__name__": "__main__"}
chunks = []

class _Stream(io.TextIOBase):
    def __init__(self, name):
        self.name = name
    def writable(self):
        return True
    def write(self, text):
        if chunks and chunks[-1]["name"] == self.name:
            chunks[-1]["text"] += text
        else:
            chunks.append({"type": "stream", "name": self.name, "text": text})
        return len(text)

sys.stdout, sys.stderr = _Stream("stdout"), _Stream("stderr")

def _error(exc, lines):
    return {"type": "error", "name": type(exc).__name__, "value": str(exc),
            "traceback": [line.rstrip("\n") for line in lines]}

def run(code, timeout):
    chunks.clear()
    tail = []
    try:
        tree = ast.parse(code, "<cell>")
    except SyntaxError as exc:
        return [_error(exc, traceback.format_exception_only(type(exc), exc))], False
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    signal.alarm(timeout)
    try:
        exec(compile(tree, "<cell>", "exec"), namespace)
        if last is not None:
            value = eval(compile(ast.Expression(last.value), "<cell>", "eval"), namespace)
            if value is not None:
                namespace["_"] = value
                tail.append({"type": "result", "data": {"text/plain": repr(value)}})
    except _Timeout:
        signal.alarm(0)
        tail.append({"type": "error", "name": "TimeoutError",
                     "value": f"Execution timed out after {timeout} seconds", "traceback": []})
        return list(chunks) + tail, True
    except BaseException as exc:
        # Skip this driver's frame so the traceback starts in the cell.
        tb = exc.__traceback__.tb_next if exc.__traceback__ else None
        tail.append(_error(exc, traceback.format_exception(type(exc), exc, tb)))
    finally:
        signal.alarm(0)
    return list(chunks) + tail, False

for line in requests:
    request = json.loads(line)
    outputs, timed_out = run(request["code"], request["timeout"])
    replies.write(json.dumps({"outputs": outputs, "timed_out": timed_out}) + "\n")
"""

# How long past the in-kernel timeout to wait before killing a kernel that
# ignores SIGALRM, such as one stuck inside a C extension.
KILL_GRACE_SECONDS = 15


class KernelLost(Exception):
    """The kernel process exited or its sandbox stopped responding."""


def executor_image() -> Image:
    return (
        Image.debian_slim("3.12")
        .apt_install(["build-essential", "graphviz", "libgraphviz-dev", "pkg-config"])
        .run_commands(
            "pip install --no-cache-dir torch==2.4.1 "
            "--index-url https://download.pytorch.org/whl/cpu"
        )
        .pip_install(EXECUTOR_PACKAGES)
    )


# ## One pooled sandbox
#
# A slot is what DSGym calls a container: a sandbox plus the kernel running in
# it. Every SDK call for a slot runs on the slot's own single worker thread.
# That serializes its cells and resets, so a restart never interleaves with a
# running cell, and it keeps each kernel's output stream on the thread that
# opened it, which the SDK's tracing requires.


class Slot:
    def __init__(self, slot_id: int, pool: SandboxPool) -> None:
        self.id = slot_id
        self.pool = pool
        self.sandbox: Sandbox | None = None
        self.process: Any = None
        self.events: Iterator[Any] | None = None
        self.restarting = False
        self.generation = 0
        self._killed = False
        self._worker = ThreadPoolExecutor(1, thread_name_prefix=f"slot-{slot_id}")

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        return self._worker.submit(fn, *args)

    def reset(self) -> None:
        """Start a fresh kernel in an empty working directory.

        The sandbox is kept if it still responds and replaced if it does not,
        so a slot whose host went away comes back with a new sandbox.
        """
        self.restarting = True
        try:
            self._stop_kernel()
            for attempt in range(2):
                if self.sandbox is None:
                    self.generation += 1
                    self.sandbox = self.pool.start_sandbox(self.id, self.generation)
                try:
                    self.sandbox.exec(
                        "sh",
                        "-c",
                        f"rm -rf {WORKDIR} && mkdir -p {WORKDIR}",
                        timeout_secs=60,
                    )
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    logger.warning(
                        "slot %d: sandbox unresponsive, replacing it", self.id
                    )
                    self._discard_sandbox()
            assert self.sandbox is not None
            self.process = self.sandbox.exec_start(
                "python", "-u", "-c", KERNEL, workdir=WORKDIR
            )
            self.events = self.process.output()
        finally:
            self.restarting = False

    def execute(self, code: str, timeout: int) -> list[dict[str, Any]]:
        if self.process is None:
            self.reset()
        self._killed = False
        watchdog = threading.Timer(timeout + KILL_GRACE_SECONDS, self._kill_kernel)
        watchdog.start()
        lost: KernelLost | None = None
        try:
            self.process.write_stdin(
                json.dumps({"code": code, "timeout": timeout}) + "\n"
            )
            reply = self._read_reply()
        except KernelLost as exc:
            lost = exc
        finally:
            watchdog.cancel()
        if lost is not None:
            # Match a Jupyter kernel that died: report it as the cell's error,
            # then give the episode a fresh kernel. State is lost.
            if self._killed:
                outputs = [_timeout_error(timeout)]
            else:
                logger.warning(
                    "slot %d: kernel lost (%s), restarting it", self.id, lost
                )
                outputs = [
                    {
                        "type": "error",
                        "name": "KernelDied",
                        "value": f"The kernel stopped and was restarted: {lost}",
                        "traceback": [],
                    }
                ]
            self.reset()
            return outputs
        # DSGym's executor restarts the kernel after a timeout, so the next
        # cell starts clean rather than inside half-finished state.
        if reply["timed_out"]:
            self.reset()
        return reply["outputs"]

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)

    def _read_reply(self) -> dict[str, Any]:
        assert self.events is not None
        buffer = ""
        try:
            for event in self.events:
                if event.stdout:
                    buffer += event.stdout
                    if "\n" in buffer:
                        return json.loads(buffer.split("\n", 1)[0])
                if event.stderr:
                    logger.debug(
                        "slot %d kernel stderr: %s", self.id, event.stderr.rstrip()
                    )
                if event.is_exited or event.is_error:
                    raise KernelLost(
                        event.error_message or f"exit code {event.exit_code}"
                    )
        except KernelLost:
            raise
        except Exception as exc:
            raise KernelLost(str(exc)) from exc
        raise KernelLost("output stream closed")

    def _kill_kernel(self) -> None:
        # Runs on the watchdog's timer thread. Sending a signal only queues a
        # request on the kernel's stream, so it is safe off the slot thread.
        process = self.process
        if process is None:
            return
        self._killed = True
        try:
            process.send_signal(signal.SIGKILL)
        except Exception:
            logger.warning("slot %d: could not kill a stuck kernel", self.id)

    def _stop_kernel(self) -> None:
        process, events = self.process, self.events
        self.process = self.events = None
        if process is None:
            return
        try:
            process.close_stdin()
            process.send_signal(signal.SIGKILL)
            for _ in events or ():
                pass
        except Exception:
            pass

    def _discard_sandbox(self) -> None:
        sandbox, self.sandbox = self.sandbox, None
        if sandbox is not None:
            self.pool.terminate(sandbox)


def _timeout_error(timeout: int) -> dict[str, Any]:
    return {
        "type": "error",
        "name": "TimeoutError",
        "value": f"Execution timed out after {timeout} seconds",
        "traceback": [],
    }


# ## The pool
#
# Every sandbox this process starts is recorded before it is started, so
# shutdown can terminate it even if startup was interrupted halfway.


class SandboxPool:
    def __init__(
        self, args: argparse.Namespace, image_uri: str, data_volume: Volume | None
    ) -> None:
        self.args = args
        self.image_uri = image_uri
        self.data_volume = data_volume
        self.run_tag = uuid.uuid4().hex[:8]
        self.slots = [Slot(i, self) for i in range(args.pool_size)]
        self.free: queue.Queue[int] = queue.Queue()
        self.allocated: set[int] = set()
        self._sandboxes: set[Sandbox] = set()
        self._sandboxes_lock = threading.Lock()
        self._closed = threading.Event()

    def start_sandbox(self, slot_id: int, generation: int) -> Sandbox:
        sandbox = Sandbox(
            image=self.image_uri,
            name=f"dsgym-{self.run_tag}-{slot_id}-{generation}",
            cpu=self.args.cpu,
            memory=self.args.memory,
            env=SANDBOX_ENV,
            volumes=[(self.data_volume, DATA_MOUNT_PATH)] if self.data_volume else None,
            network_policy=NetworkPolicy() if self.args.no_network else None,
            lifetime=self.args.sandbox_lifetime,
        )
        with self._sandboxes_lock:
            if self._closed.is_set():
                raise RuntimeError("pool is shutting down")
            self._sandboxes.add(sandbox)
        try:
            return sandbox.run(timeout=900)
        except BaseException:
            self.terminate(sandbox)
            raise

    def terminate(self, sandbox: Sandbox) -> None:
        with self._sandboxes_lock:
            self._sandboxes.discard(sandbox)
        if sandbox.id is None:
            return
        try:
            sandbox.terminate()
        except SandboxNotFoundError:
            pass  # Already gone, for example because its host went away.
        except Exception:
            logger.exception("could not terminate sandbox %s", sandbox.id)

    def start(self) -> None:
        """Start every slot in parallel, then serve with however many came up.

        A slot that fails to start keeps retrying in the background and joins
        the pool when it succeeds.
        """
        started = time.monotonic()
        futures = {slot.id: slot.submit(self.try_reset, slot) for slot in self.slots}
        wait(futures.values())
        ready = [slot for slot in self.slots if futures[slot.id].result()]
        logger.info(
            "%d/%d sandboxes ready in %.0fs",
            len(ready),
            len(self.slots),
            time.monotonic() - started,
        )
        for slot in self.slots:
            if slot in ready:
                self.free.put(slot.id)
            else:
                self.recycle(slot)

    def recycle(self, slot: Slot) -> None:
        """Reset a slot off the request path, retrying until it can join the pool."""
        slot.submit(self._revive, slot)

    def try_reset(self, slot: Slot) -> bool:
        try:
            slot.reset()
            return True
        except Exception:
            logger.exception("slot %d: could not start", slot.id)
            return False

    def _revive(self, slot: Slot) -> None:
        delay = 5.0
        while not self._closed.is_set():
            if self.try_reset(slot):
                self.free.put(slot.id)
                return
            self._closed.wait(delay)
            delay = min(delay * 2, 120.0)

    def close(self) -> None:
        with self._sandboxes_lock:
            self._closed.set()
            sandboxes = list(self._sandboxes)
        for slot in self.slots:
            slot.close()
        logger.info("terminating %d sandboxes", len(sandboxes))
        with ThreadPoolExecutor(max(1, min(32, len(sandboxes)))) as executor:
            list(executor.map(self.terminate, sandboxes))


# ## DSGym's manager API
#
# Routes, request bodies, and response shapes match DSGym's
# executors/manager/main.py.


class CodeRequest(BaseModel):
    code: str


def create_app(pool: SandboxPool) -> FastAPI:
    args = pool.args

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Waiting /allocate calls each hold a default-executor thread, so size
        # it for every episode to wait at once.
        workers = len(pool.slots) + 8
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(workers))
        yield

    app = FastAPI(title="DSGym executors on Chalk sandboxes", lifespan=lifespan)

    def slot_for(container_id: int) -> Slot:
        if not 0 <= container_id < len(pool.slots):
            raise HTTPException(404, f"Container {container_id} not found")
        return pool.slots[container_id]

    def allocated_slot(container_id: int) -> Slot:
        slot = slot_for(container_id)
        if container_id not in pool.allocated:
            raise HTTPException(
                400, f"Container {container_id} is not allocated or invalid"
            )
        return slot

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/allocate")
    async def allocate(container_type: str | None = None) -> dict[str, int]:
        if container_type not in (None, CONTAINER_TYPE):
            raise HTTPException(400, f"Unknown container type: {container_type}")
        # DSGym's manager fails at once when no container is free; waiting
        # briefly lets an episode reuse a slot that is being reset.
        try:
            slot_id = await asyncio.to_thread(
                pool.free.get, True, args.allocate_timeout
            )
        except queue.Empty:
            raise HTTPException(503, "No containers available") from None
        pool.allocated.add(slot_id)
        return {"container_id": slot_id}

    @app.post("/deallocate/{container_id}")
    async def deallocate(container_id: int) -> dict[str, Any]:
        slot = slot_for(container_id)
        if container_id not in pool.allocated:
            raise HTTPException(400, f"Container {container_id} is not allocated")
        pool.allocated.discard(container_id)
        pool.recycle(slot)
        return {"status": "deallocated", "container_id": container_id}

    @app.post("/restart")
    async def restart_allocated() -> dict[str, Any]:
        ids = sorted(pool.allocated)
        if not ids:
            return {"status": "no_containers_to_restart", "restarted_containers": []}
        for container_id in ids:
            pool.allocated.discard(container_id)
            pool.recycle(pool.slots[container_id])
        return {
            "status": "restart_completed",
            "restarted_containers": ids,
            "failed_containers": [],
            "total_restarted": len(ids),
            "total_failed": 0,
        }

    @app.post("/session/{container_id}/execute")
    async def execute(container_id: int, request: CodeRequest) -> dict[str, Any]:
        slot = allocated_slot(container_id)
        future = slot.submit(slot.execute, request.code, args.execution_timeout)
        try:
            outputs = await asyncio.wrap_future(future)
        except Exception as exc:
            raise HTTPException(
                503, f"Container {container_id} is not available: {exc}"
            ) from exc
        return {"outputs": outputs}

    @app.get("/session/{container_id}/health")
    async def session_health(container_id: int) -> dict[str, str]:
        slot_for(container_id)
        return {"status": "ok"}

    @app.get("/session/{container_id}/ready")
    async def session_ready(container_id: int) -> dict[str, Any]:
        slot = slot_for(container_id)
        if slot.restarting:
            return {"ready": False, "reason": "kernel_restarting"}
        if slot.process is None:
            return {"ready": False, "reason": "kernel_not_started"}
        return {"ready": True}

    @app.post("/session/{container_id}/restart")
    async def session_restart(container_id: int) -> dict[str, str]:
        slot = slot_for(container_id)
        slot.submit(pool.try_reset, slot)
        return {"status": "restarting"}

    @app.get("/session/{container_id}/restart-status")
    async def session_restart_status(container_id: int) -> dict[str, bool]:
        return {"restarting": slot_for(container_id).restarting}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        available = pool.free.qsize()
        return {
            "available_containers": available,
            "available_by_type": {CONTAINER_TYPE: available},
            "allocated_containers": len(pool.allocated),
            "total_containers": len(pool.slots),
            "allocated_container_ids": sorted(pool.allocated),
        }

    return app


# ## Task data
#
# Sandboxes mount the data volume pinned to its latest version at startup, so
# they see an immutable snapshot even if the volume changes later.


def resolve_data_volume(args: argparse.Namespace) -> Volume | None:
    if args.data_volume is None:
        if args.upload_data is not None:
            raise SystemExit("--upload-data requires --data-volume")
        logger.warning(
            "no --data-volume: tasks that read files under %s will fail",
            DATA_MOUNT_PATH,
        )
        return None
    client = VolumeClient.from_env()
    if args.upload_data is not None:
        source = Path(args.upload_data)
        if not source.is_dir():
            raise SystemExit(f"--upload-data {source} is not a directory")
        volume = (
            client.lookup(args.data_volume)
            if client.exists(args.data_volume)
            else client.create(args.data_volume)
        )
        logger.info(
            "uploading %s to volume %s (kept after exit)", source, args.data_volume
        )
        volume.put_dir(source)
    else:
        volume = client.from_name(args.data_volume)
    return volume.at_version(volume.latest_version().id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--pool-size",
        type=int,
        default=8,
        help="sandboxes to keep; set it to the number of concurrent episodes (default: 8)",
    )
    parser.add_argument(
        "--data-volume",
        help="Chalk volume holding DSGym's data/data files, mounted at /data",
    )
    parser.add_argument(
        "--upload-data",
        metavar="DIR",
        help="upload this directory to --data-volume first, creating the volume if needed",
    )
    parser.add_argument(
        "--image",
        help="use this image reference instead of building the executor image",
    )
    parser.add_argument("--cpu", default="2", help="CPU per sandbox (default: 2)")
    parser.add_argument(
        "--memory", default="4Gi", help="memory per sandbox (default: 4Gi)"
    )
    parser.add_argument(
        "--execution-timeout",
        type=int,
        default=180,
        help="seconds a cell may run before it is stopped, as in DSGym (default: 180)",
    )
    parser.add_argument(
        "--allocate-timeout",
        type=float,
        default=30.0,
        help="seconds /allocate waits for a free sandbox (default: 30)",
    )
    parser.add_argument(
        "--sandbox-lifetime",
        default="21600s",
        help="server-side lifetime cap per sandbox, a fallback if this process is killed "
        "(default: 21600s)",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="block outbound traffic from the sandboxes",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to serve on (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="port to serve on, DSGym's default (default: 5000)",
    )
    args = parser.parse_args()
    if args.pool_size < 1:
        parser.error("--pool-size must be at least 1")
    return args


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args()
    data_volume = resolve_data_volume(args)
    if args.image:
        image_uri = args.image
    else:
        logger.info("building the executor image (cached after the first build)")
        image_uri = chalkcompute.build_image(executor_image())
    pool = SandboxPool(args, image_uri, data_volume)
    try:
        pool.start()
        logger.info("serving DSGym's manager API on http://%s:%d", args.host, args.port)
        uvicorn.run(
            create_app(pool), host=args.host, port=args.port, log_level="warning"
        )
    finally:
        pool.close()


if __name__ == "__main__":
    main()
