"""Training container entrypoint.

Invoked as::

    python -m chalkcompute._training_entrypoint

This module lives outside ``chalkcompute/training/`` so running it never imports
that subpackage, which would shadow the ``chalkcompute.training`` decorator that
user modules apply at import time. ``chalkcompute.training.entrypoint`` remains a
shim for servers that predate ``CreateTrainingRunRequest.entrypoint`` and still
invoke the old path.

Env vars set by ``buildContainerSpec`` (server-side):

    CHALK_TRAINING_RUN_ID          — UUID of the training run
    CHALK_TRAINING_RUN_NAME        — human-readable run name
    CHALK_TRAINING_MODULE          — dotted path, e.g. "my_project.train_spam"
    CHALK_DATA_DIR                 — data volume mount (default /chalk/data)
    CHALK_CHECKPOINT_DIR           — checkpoint volume mount (default /chalk/checkpoints)
    CHALK_DATASET_REVISION_ID      — immutable dataset revision to load
    CHALK_DATA_VOLUME_NAME         — name of the data volume (server-assigned)
    CHALK_MAX_RETRIES              — max retry attempts (0 = no retries)
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
import os
import sys
import traceback
import typing
from pathlib import Path
from typing import Any

from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.server.v1 import (
    training_runs_connect as tr_connect,
    training_runs_pb as tr_pb,
)

logger = logging.getLogger("chalkcompute.training.entrypoint")


def _update_status(
    client: ConnectClient,
    run_id: str,
    status: tr_pb.TrainingRunStatus,
    *,
    error_message: str | None = None,
) -> None:
    paths = ["status"]
    update = tr_pb.UpdateTrainingRunOperation(status=status)
    if error_message is not None:
        update = tr_pb.UpdateTrainingRunOperation(
            status=status, error_message=error_message
        )
        paths.append("error_message")

    stub = client.stub(tr_connect.TrainingRunServiceClientSync)
    client.call(
        stub.update_training_run,
        tr_pb.UpdateTrainingRunRequest(
            training_run_id=run_id,
            update=update,
            update_mask=FieldMask(paths=paths),
        ),
    )


def _get_training_run(client: ConnectClient, run_id: str) -> tr_pb.TrainingRun:
    stub = client.stub(tr_connect.TrainingRunServiceClientSync)
    resp = client.call(
        stub.get_training_run,
        tr_pb.GetTrainingRunRequest(training_run_id=run_id),
    )
    if resp.training_run is None:
        raise RuntimeError(f"GetTrainingRun returned no run for {run_id}")
    return resp.training_run


def _discover_function(module_path: str) -> Any:
    parts = module_path.rsplit(".", 1)
    if len(parts) != 2:
        raise RuntimeError(
            f"CHALK_TRAINING_MODULE must be <module>.<function>, got {module_path!r}"
        )
    mod_name, func_name = parts
    mod = importlib.import_module(mod_name)
    func = getattr(mod, func_name, None)
    if func is None:
        raise RuntimeError(f"Function {func_name!r} not found in module {mod_name!r}")
    return func


def _deserialize_config(config_struct: object | None, func: object) -> object:
    if config_struct is None:
        return None

    config_dict: dict = config_struct.to_python()  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

    underlying = getattr(func, "_func", func)
    try:
        hints = typing.get_type_hints(underlying)
    except Exception:
        return config_dict

    sig = inspect.signature(underlying)  # ty: ignore[invalid-argument-type]
    params = list(sig.parameters.keys())
    if len(params) >= 2:
        config_type = hints.get(params[1])
        if (
            config_type is not None
            and dataclasses.is_dataclass(config_type)
            and isinstance(config_type, type)
        ):
            try:
                resolved_hints = typing.get_type_hints(config_type)
                coerced = {}
                for f in dataclasses.fields(config_type):
                    if f.name not in config_dict:
                        continue
                    val = config_dict[f.name]
                    hint = resolved_hints.get(f.name)
                    if hint is int and isinstance(val, float):
                        val = int(val)
                    elif hint is bool and isinstance(val, (int, float)):
                        val = bool(val)
                    coerced[f.name] = val
                return config_type(**coerced)
            except Exception:
                logger.warning(
                    "Could not instantiate %s from config, using dict",
                    config_type.__name__,
                    exc_info=True,
                )

    return config_dict


def _load_dataset(
    client: ConnectClient, volume_name: str, revision_id: str, data_dir: str
) -> None:
    from chalkcompute._volume import Volume, VolumeClient

    logger.info("Loading dataset revision %s into volume %s", revision_id, volume_name)
    vclient = VolumeClient.from_connect(client)
    vol = Volume(volume_name, client=vclient)
    vol.load_dataset(revision_id)
    logger.info("Dataset loaded into %s", volume_name)

    ctl_path = Path(data_dir) / ".chalk-volume" / "ctl"
    if ctl_path.exists():
        with open(ctl_path, "r+b") as ctl:
            ctl.write(b"reload\n")
            ctl.flush()
            response = ctl.read().decode(errors="replace").strip()
        status, _, message = response.partition("\n")
        if status == "ok":
            logger.info("FUSE reload: %s", message)
        else:
            logger.warning("FUSE reload failed: %s", response)


def _load_dataframe(data_dir: str) -> object:
    parquet_files = sorted(str(p) for p in Path(data_dir).rglob("*.parquet"))
    if not parquet_files:
        logger.warning("No parquet files found in %s", data_dir)
        return None

    try:
        import polars as pl

        return pl.scan_parquet(parquet_files)
    except ImportError:
        pass

    try:
        import pandas as pd  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]

        frames = [pd.read_parquet(f) for f in parquet_files]
        return pd.concat(frames, ignore_index=True) if frames else None
    except ImportError:
        pass

    logger.warning("Neither polars nor pandas available; passing data_dir path")
    return data_dir


def _commit_retry_count(volume_name: str, path: str, count: int) -> None:
    from chalkcompute._volume import Volume

    vol = Volume(volume_name, create_if_missing=False)
    vol.put_file(path, str(count))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    run_id = os.environ.get("CHALK_TRAINING_RUN_ID")
    if not run_id:
        logger.error("CHALK_TRAINING_RUN_ID is not set")
        sys.exit(1)

    module_path = os.environ.get("CHALK_TRAINING_MODULE")
    if not module_path:
        logger.error("CHALK_TRAINING_MODULE is not set")
        sys.exit(1)

    data_dir = os.environ.get("CHALK_DATA_DIR", "/chalk/data")
    checkpoint_dir = os.environ.get("CHALK_CHECKPOINT_DIR", "/chalk/checkpoints")
    revision_id = os.environ.get("CHALK_DATASET_REVISION_ID", "")
    max_retries = int(os.environ.get("CHALK_MAX_RETRIES", "0"))

    os.environ.setdefault("CHALK_CHECKPOINT_DIR", checkpoint_dir)

    logger.info("Training entrypoint starting for run %s", run_id)

    retry_count = 0
    retry_marker_path = f".retry_count_{run_id}"
    ckpt_vol_name = ""
    if max_retries > 0:
        run_name = os.environ.get("CHALK_TRAINING_RUN_NAME", "")
        ckpt_vol_name = f"training-ckpt-{run_name}"
        retry_marker_local = Path(checkpoint_dir) / retry_marker_path
        if retry_marker_local.exists():
            try:
                retry_count = int(retry_marker_local.read_text().strip())
            except (ValueError, OSError):
                retry_count = 0

    client = ConnectClient()

    if max_retries > 0 and retry_count >= max_retries:
        try:
            _update_status(client, run_id, tr_pb.TrainingRunStatus.WORKING)
        except Exception:
            pass
        try:
            _update_status(
                client,
                run_id,
                tr_pb.TrainingRunStatus.FAILED,
                error_message=f"Exhausted {max_retries} retries",
            )
        except Exception:
            pass
        logger.info("Retries exhausted (%d/%d), exiting", retry_count, max_retries)
        sys.exit(0)

    if max_retries > 0:
        _commit_retry_count(ckpt_vol_name, retry_marker_path, retry_count + 1)
        if retry_count > 0:
            logger.info("Retry attempt %d of %d", retry_count, max_retries)

    try:
        _update_status(client, run_id, tr_pb.TrainingRunStatus.WORKING)
        logger.info("Status set to WORKING")

        run = _get_training_run(client, run_id)

        func = _discover_function(module_path)
        logger.info("Discovered training function: %s", module_path)

        config = _deserialize_config(run.config, func)

        if revision_id:
            data_volume_name = os.environ.get(
                "CHALK_DATA_VOLUME_NAME", f"training-data-{run_id[:8]}"
            )
            _load_dataset(client, data_volume_name, revision_id, data_dir)

        df = _load_dataframe(data_dir)

        logger.info("Invoking training function")
        result = func(df, config)

        if result is not None:
            logger.info(
                "Training function returned a value (type=%s)",
                type(result).__name__,
            )

        if max_retries > 0:
            _commit_retry_count(ckpt_vol_name, retry_marker_path, 0)

        _update_status(client, run_id, tr_pb.TrainingRunStatus.COMPLETED)
        logger.info("Training run %s completed", run_id)

    except SystemExit:
        raise
    except Exception:
        error_msg = traceback.format_exc()
        logger.error("Training run %s failed:\n%s", run_id, error_msg)
        if max_retries > 0 and retry_count < max_retries:
            logger.info(
                "%d/%d retries remaining, exiting for restart",
                max_retries - retry_count - 1,
                max_retries,
            )
            sys.exit(1)
        try:
            _update_status(client, run_id, tr_pb.TrainingRunStatus.WORKING)
        except Exception:
            pass
        try:
            _update_status(
                client,
                run_id,
                tr_pb.TrainingRunStatus.FAILED,
                error_message=error_msg,
            )
        except Exception:
            logger.exception("Failed to report error status to server")
        sys.exit(1)


if __name__ == "__main__":
    main()
