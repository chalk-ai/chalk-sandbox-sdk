"""One-shot tiny synthetic pipeline; called only by a reviewed frozen entrypoint."""

from __future__ import annotations

import os
import time
from pathlib import Path


def run(config):
    import importlib
    from typing import Any, cast

    import torch
    import chalkcompute as cc

    WeightStream = cast(
        Any, importlib.import_module("chalkcompute.weights")
    ).WeightStream
    from profile_streaming import generate
    from profile_sharded import prepare
    from profile_sharded_metrics import (
        Measurements,
        REMOTE_FILE_RESERVE,
        MAX_FIXTURE_BYTES,
        tree_bytes,
    )
    from profile_fixture import check_workspace

    torch.set_num_threads(4)
    root = Path(config["payload_root"])
    if time.time() >= config["work_deadline_epoch"]:
        raise TimeoutError("Guest reached cleanup reserve before fixture generation")
    measure = Measurements(root)
    measure.sample("imports_complete")
    if os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < 512 * 1024**2:
        raise RuntimeError("Guest available disk below512MiB small-fixture gate")
    quota_bytes = 128 * 1024**2
    before_quota = tree_bytes(root)
    if (
        max(before_quota.values()) + quota_bytes + REMOTE_FILE_RESERVE
        > MAX_FIXTURE_BYTES
    ):
        raise RuntimeError("Quota probe and owned files exceed small-fixture disk cap")
    workspace = check_workspace(root, quota_bytes)
    workspace["conservative_peak_owned_bytes_during_probe"] = (
        max(before_quota.values()) + quota_bytes
    )
    workspace["scope"] = (
        "actual nonsparse128MiB quota probe before generation/uploads; released before fixture data"
    )
    measure.sample("quota_probe_released")
    source = root / "source"
    # Fixed16MiB input. Four4MiB shards exercise actual grouped SDK uploads.
    generate(source, tensors=16, elements=524288, shard_tensors=4)
    measure.sample("source_generated")
    stream = WeightStream(
        config["name"],
        "synthetic/sharded-fixture",
        volume=cc.Volume(config["name"], create_if_missing=False),
    ).create()
    reference = WeightStream(
        config["name"] + "-reference",
        "synthetic/sharded-fixture",
        volume=cc.Volume(config["name"] + "-reference", create_if_missing=False),
    ).create()
    began = time.perf_counter()

    def sample(stage):
        if time.time() >= config["work_deadline_epoch"]:
            raise TimeoutError("Guest reached120-second cleanup reserve")
        measure.sample(stage)

    proof = prepare(
        stream,
        reference,
        source,
        root / "fixture",
        expected_tensors=16,
        expected_elements=8388608,
        sample=sample,
    )
    # Conservative sum of all publication inputs, counting repeated metadata
    # per historical snapshot; excludes small backend object-format overhead.
    remote_bytes = sum(p.stat().st_size for p in source.iterdir() if p.is_file())
    remote_bytes += sum(
        p.stat().st_size for p in (root / "fixture/reference").iterdir() if p.is_file()
    )
    remote_bytes += (root / "fixture/patch/patches.bin").stat().st_size
    remote_bytes += (
        sum(
            (root / "fixture" / name).stat().st_size
            for name in (
                "baseline-attempt.json",
                "patch-attempt.json",
                "reference-attempt.json",
            )
        )
        + 4096
    )
    if remote_bytes > REMOTE_FILE_RESERVE:
        raise RuntimeError("Remote fixture publication bytes exceed48MiB reserve")
    measure.sample("complete")
    proof.update(
        plan_sha256=config["plan_sha256"],
        volume_name=config["name"],
        reference_volume_name=config["name"] + "-reference",
        measurements=measure.result(),
        workspace=workspace,
        conservative_remote_publication_bytes=remote_bytes,
        pipeline_seconds=time.perf_counter() - began,
        fixture_is_real_model=False,
        gpu_claimed=False,
        full_8b_projection_measured=False,
    )
    # Compute teardown owns all temporary fixture/cache bytes; do not delete
    # evidence while the parent is still polling this one-shot entrypoint.
    return proof
