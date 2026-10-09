"""Compare generated Python/native handlers, IPC, and SQL -> real Rust gRPC server.

Requires the matching static_accelerator checkout, chalkdf, and pyperf. Example:
  PYTHONPATH=. python benches/bench_static_acceleration.py --transport ipc --fast
  PYTHONPATH=. python benches/bench_static_acceleration.py --transport sql --rows 10000 --fast
Compilation/server startup and correctness checks are outside the timed region.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import types

import chalkdf
import grpc
import pyarrow as pa
import pyperf
from chalk_remote_call.arrow_utils import decode_ipc_stream, encode_record_batch
from chalk_remote_call.servicer import process_batches
from chalkcompute._function import _generate_handler_shim


def score(x: float) -> float:
    if x > 50.0:
        return x * 1.5 + 2.0
    return x * 0.75 - 1.0


def normalize(x: str) -> str:
    return x.strip().lower().replace("-", "_")


def make_handler(function, accelerated):
    module = types.ModuleType("_acceleration_benchmark_user")
    module.function = function
    sys.modules[module.__name__] = module
    previous = os.environ.get("CHALK_FUNCTION_STATIC_ACCELERATION")
    os.environ["CHALK_FUNCTION_STATIC_ACCELERATION"] = "1" if accelerated else "0"
    try:
        namespace = {}
        exec(_generate_handler_shim("function", module_name=module.__name__), namespace)
    finally:
        if previous is None:
            os.environ.pop("CHALK_FUNCTION_STATIC_ACCELERATION", None)
        else:
            os.environ["CHALK_FUNCTION_STATIC_ACCELERATION"] = previous
    if accelerated:
        assert namespace["_accelerated"] is not None, "function did not compile"
    return namespace["handler"]


@contextlib.contextmanager
def server(function_name, accelerated):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve",
            str(port),
            function_name,
            str(int(accelerated)),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    address = f"127.0.0.1:{port}"
    try:
        with grpc.insecure_channel(address) as channel:
            grpc.channel_ready_future(channel).result(timeout=20)
        yield address
    finally:
        process.terminate()
        process.wait(timeout=10)


def sql_call(address, data, output_type):
    from libchalk.binding import ChalkSqlCatalogBindInfo, PyBindingRegistry
    from libchalk.chalksql import ChalkSqlCatalog, sql_to_table
    from libchalk.chalktable import CompilationOptions, CompiledPlan
    from libchalk.runtime import EngineWrapperCatalogSchemaService
    from libchalk.runtime.remote_call import CatalogRuntime, RemoteCallClientSupplier

    class Schema:
        def get_schema(self, name):
            assert name == "bench.function"
            return output_type

    catalog = ChalkSqlCatalog()
    catalog.register_constant_table("inputs", pa.table(data))
    catalog.set_catalog_schema_service(EngineWrapperCatalogSchemaService(Schema()))
    registry = PyBindingRegistry()
    registry.register_chalk_sql_catalog(catalog)
    runtime = CatalogRuntime("acceleration_benchmark")
    runtime.register_function(
        "bench.function",
        RemoteCallClientSupplier.from_grpc_host(address),
        "function",
        output_type,
    )
    table = sql_to_table(
        catalog,
        ChalkSqlCatalogBindInfo.create(),
        "benchmark",
        "SELECT catalog_call('bench.function', x) AS result FROM inputs",
        binding_registry=registry,
    )
    plan = CompiledPlan(
        "velox",
        CompilationOptions(),
        (table,),
        binding_registry=registry,
        catalog_runtime=runtime,
    )
    context = chalkdf.DataFrame.from_arrow(pa.table(data))._run_context()

    def run():
        return (
            pa.Table.from_batches(plan.run(context, {}, {}).result().batches)
            .column(0)
            .to_pylist()
        )

    return run


def main():
    def worker_args(command, args):
        command.extend(
            [
                "--transport",
                args.transport,
                "--rows",
                str(args.rows),
                "--function",
                args.function,
            ]
        )

    runner = pyperf.Runner(add_cmdline_args=worker_args)
    runner.argparser.add_argument(
        "--transport", choices=("handler", "ipc", "sql"), default="handler"
    )
    runner.argparser.add_argument("--rows", type=int, default=10000)
    runner.argparser.add_argument(
        "--function", choices=("score", "normalize"), default="score"
    )
    args = runner.parse_args()
    function = globals()[args.function]
    data = (
        {"x": pa.array([float(i % 100) for i in range(args.rows)])}
        if function is score
        else {"x": pa.array(["  USER-Name-Example  "] * args.rows)}
    )
    output_type = pa.float64() if function is score else pa.string()
    expected = [function(x) for x in data["x"].to_pylist()]
    ipc = encode_record_batch(pa.record_batch(data))
    with contextlib.ExitStack() as stack:
        for accelerated in (False, True):
            start = time.perf_counter()
            handler = make_handler(function, accelerated)
            setup_ms = (time.perf_counter() - start) * 1000
            if args.transport == "sql":
                address = stack.enter_context(server(args.function, accelerated))
                run = sql_call(address, data, output_type)
                assert run() == expected
            elif args.transport == "ipc":

                def run(handler=handler):
                    return process_batches(ipc, handler, ["x"], {})

                actual = [
                    x
                    for payload in run()
                    for batch in decode_ipc_stream(payload)
                    for x in batch.column(0).to_pylist()
                ]
                assert actual == expected
            else:

                def run(handler=handler):
                    result = handler(data, {})
                    return (
                        result
                        if isinstance(result, pa.Array)
                        else pa.array(result, type=output_type)
                    )

                assert run().to_pylist() == expected
            mode = "native" if accelerated else "python"
            runner.bench_func(
                f"{args.transport}_{args.function}_{args.rows}_{mode}",
                run,
                metadata={"handler_setup_ms": setup_ms, "rows": args.rows},
            )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        from chalk_remote_call.server import serve

        serve(
            make_handler(globals()[sys.argv[3]], bool(int(sys.argv[4]))),
            host="127.0.0.1",
            port=int(sys.argv[2]),
            workers=4,
            arg_names=["x"],
        )
    else:
        main()
