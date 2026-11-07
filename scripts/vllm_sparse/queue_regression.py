"""CPU-only execution of the pinned executor's actual RPC/FutureWrapper AST.

No vLLM imports, GPU calls, network access, processes, or resource operations.
The fake queues replace transport only. Run with --source to another saved copy
of the exact reviewed upstream file; its SHA256 is required to match.
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures
import contextlib
import enum
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

SOURCE_COMMIT = "4ba787ec9959e566d3b587a796dff07673324da5"
SOURCE_SHA256 = "d1de4acfa99d63325c7449e61aef24dbe96c16291ef7347836695faee8faa3fc"
CHECKSUM_ERROR = "Checksum mismatch for language_model.model.norm.weight"


class Status(enum.Enum):
    SUCCESS = 1
    FAILURE = 2
    FAILURE_WITH_KV_OUTPUT = 3


class Queue:
    def __init__(self):
        self.messages = collections.deque()

    def dequeue(self, timeout=None):
        return self.messages.popleft()


def extract(source):
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Source differs from the pinned reviewed executor")
    tree = ast.parse(source.read_text())
    future = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "FutureWrapper"
    )
    executor = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "MultiprocExecutor"
    )
    method = next(
        n
        for n in executor.body
        if isinstance(n, ast.FunctionDef) and n.name == "collective_rpc"
    )
    # Remove typing-only decorators; preserve the method/FutureWrapper bodies.
    method.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            future,
            method,
        ],
        type_ignores=[],
    )
    namespace = {
        "Future": concurrent.futures.Future,
        "suppress": contextlib.suppress,
        "InvalidStateError": concurrent.futures.InvalidStateError,
        "WorkerProc": SimpleNamespace(ResponseStatus=Status),
        "time": time,
    }
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)  # noqa: S102 -- exact SHA256-verified reviewed source only
    return namespace["collective_rpc"]


def scenario(call, explicit_receipts):
    queues = [Queue() for _ in range(8)]

    class Broadcast:
        def enqueue(self, request):
            for rank, queue in enumerate(queues):
                if request[0] == "corrupt_prepare":
                    message = (
                        (
                            Status.SUCCESS,
                            {"rank": rank, "refused": True, "reason": CHECKSUM_ERROR},
                        )
                        if explicit_receipts
                        else (Status.FAILURE, CHECKSUM_ERROR)
                    )
                else:
                    message = (Status.SUCCESS, {"rank": rank, "aborted": True})
                queue.messages.append(message)

    executor = SimpleNamespace(
        rpc_broadcast_mq=Broadcast(),
        is_failed=False,
        response_mqs=queues,
        vllm_config=SimpleNamespace(
            parallel_config=SimpleNamespace(enable_fault_tolerance=False)
        ),
        futures_queue=collections.deque(),
    )
    results = []
    for operation in ("corrupt_prepare", "abort"):
        try:
            response = call(executor, operation)
            result = {
                "operation": operation,
                "success": True,
                "response_count": len(response),
            }
        except RuntimeError as error:
            result = {"operation": operation, "success": False, "error": str(error)}
        result["queued_per_rank"] = [len(queue.messages) for queue in queues]
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "tests/fixtures/vllm_v0330_multiproc_executor.txt",
    )
    args = parser.parse_args()
    call = extract(args.source)
    raised = scenario(call, explicit_receipts=False)
    receipts = scenario(call, explicit_receipts=True)
    assert [r["success"] for r in raised] == [False, False]
    assert raised[0]["queued_per_rank"] == [0, 1, 1, 1, 1, 1, 1, 1]
    assert raised[1]["queued_per_rank"] == [0, 1, 2, 2, 2, 2, 2, 2]
    assert all(
        r["success"] and r["response_count"] == 8 and r["queued_per_rank"] == [0] * 8
        for r in receipts
    )
    print(
        json.dumps(
            {
                "source_commit": SOURCE_COMMIT,
                "source_sha256": SOURCE_SHA256,
                "raised_worker_errors": raised,
                "successful_refusal_receipts": receipts,
                "scope": "Exact executor queue behavior only; receipt correctness needs separate actual SDK tests.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
