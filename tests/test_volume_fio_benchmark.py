from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
import yaml

from e2etests.volume.benchmarks import harness
from e2etests.volume.benchmarks.cases.fio import cli as fio
from e2etests.volume.benchmarks.cases.fio.providers import PROVIDERS, Provider
from e2etests.volume.benchmarks.cases.fio.report import compare, render_table
from e2etests.volume.benchmarks.cases.fio.workload import (
    COMPLETION_MARKER,
    ENTRYPOINT,
    FioConfig,
    FioResult,
    RESULT_NAMES,
    WRITE_RESULTS,
    build_pyperf_results,
    fio_result_to_benchmark,
)


def raw_results():
    return {
        name: {
            "fio version": "fio-3.39",
            "jobs": [
                {
                    "error": 0,
                    "write" if name in WRITE_RESULTS else "read": {
                        "runtime": 2000,
                        "io_bytes": 8 * 1024**2,
                        "bw_bytes": 4 * 1024**2,
                        "iops": 1024,
                        "clat_ns": {"percentile": {"95.000000": 2_000_000}},
                    },
                }
            ],
        }
        for name in RESULT_NAMES
    }


def result(provider="chalk"):
    return FioResult(
        results=raw_results(),
        log=COMPLETION_MARKER,
        metadata={"volume_provider": provider},
        timings={"volume-commit" if provider == "chalk" else "volume-sync": 0.5},
    )


def document(provider="chalk"):
    return build_pyperf_results(result(provider), FioConfig())


def test_conversion_rejects_partial_io_failure():
    raw = raw_results()["sequential-write"]
    raw["jobs"][0]["error"] = 22
    with pytest.raises(ValueError, match="I/O error"):
        fio_result_to_benchmark("sequential-write", raw)


def test_table_and_comparison_units_and_ratios():
    chalk, baseline = document(), document("provider-b")
    chalk["benchmarks"][0]["metadata"]["fio_bw_bytes_per_sec"] *= 2
    chalk["benchmarks"][0]["runs"][0]["values"] = [1.0]
    table = render_table(chalk)
    assert "| sequential-write | 1.000 | 8.000 | 1024.000 | 2.000 |" in table
    comparison = compare(chalk, baseline)
    assert "| sequential-write | MiB/s | higher | 8.000 | 4.000 | 2.000 |" in comparison
    assert (
        "| sequential-write | Seconds | lower | 1.000 | 2.000 | 0.500 |" in comparison
    )
    assert "| volume-commit |" not in comparison
    assert "| volume-sync |" not in comparison


@pytest.mark.parametrize(
    "key,value",
    [("fio_direct", 0), ("fio_size", "64M"), ("fio_payload_sha256", "different")],
)
def test_comparison_rejects_incompatible_configuration(key, value):
    baseline = document("provider-b")
    baseline["metadata"][key] = value
    with pytest.raises(ValueError, match=key):
        compare(document(), baseline)


def test_comparison_rejects_version_mismatch_and_missing_workload():
    baseline = document("provider-b")
    baseline["benchmarks"][0]["metadata"]["fio_version"] = "fio-other"
    with pytest.raises(ValueError, match="fio_version"):
        compare(document(), baseline)
    baseline["benchmarks"].pop(0)
    with pytest.raises(ValueError, match="Missing FIO"):
        compare(document(), baseline)


def test_comparison_handles_missing_percentiles_without_inventing_zeros():
    baseline = document("provider-b")
    del baseline["benchmarks"][0]["metadata"]["fio_clat_p95_ns"]
    assert "| p95 ms | lower | 2.000 | n/a | n/a |" in compare(document(), baseline)


def test_comparison_rejects_failed_status():
    baseline = document("provider-b")
    harness.append_status(baseline, request_type="volume-fio", error_count=1)
    with pytest.raises(ValueError, match="failed benchmark"):
        compare(document(), baseline)


@pytest.mark.parametrize(
    "candidate_provider,baseline_provider",
    [("provider-a", "provider-b"), ("provider-a", "provider-a")],
)
def test_comparison_accepts_arbitrary_providers(candidate_provider, baseline_provider):
    table = compare(document(candidate_provider), document(baseline_provider))
    assert f"Candidate ({candidate_provider})" in table
    assert f"Baseline ({baseline_provider})" in table
    assert "Candidate / baseline" in table


def test_comparison_merges_run_metadata_and_averages_measurements():
    candidate = document("provider-a")
    baseline = document("provider-b")
    candidate["benchmarks"][0]["runs"] = [
        {"values": [1.0], "metadata": {"fio_bw_bytes_per_sec": 8 * 1024**2}},
        {"values": [3.0]},
    ]
    table = compare(candidate, baseline)
    assert "| MiB/s | higher | 6.000 | 4.000 | 1.500 |" in table
    assert "| Seconds | lower | 2.000 | 2.000 | 1.000 |" in table


def test_comparison_rejects_mixed_run_configurations():
    candidate = document("provider-a")
    candidate["benchmarks"][0]["runs"] = [
        {"values": [1.0], "metadata": {"fio_direct": 0}},
        {"values": [3.0]},
    ]
    with pytest.raises(ValueError, match="Mixed configurations"):
        compare(candidate, document("provider-b"))


@pytest.mark.parametrize("provider", ["chalk", "provider-new"])
def test_manual_run_writes_json_and_table_without_chalk_archive(
    tmp_path, monkeypatch, provider
):
    output = tmp_path / "nested" / "results.json"
    run_chalk = MagicMock(return_value=result())
    run_provider = MagicMock(return_value=result("provider-new"))
    archive = MagicMock(side_effect=AssertionError("must not archive"))
    monkeypatch.setitem(PROVIDERS, "chalk", Provider(run_chalk))
    monkeypatch.setitem(
        PROVIDERS,
        "provider-new",
        Provider(
            run_provider,
            dependency_group="provider-dependencies",
            supports_placement=True,
        ),
    )
    monkeypatch.setattr(harness, "archive_benchmark_results", archive)
    fio.cli(["--provider", provider, "--local", "--output", str(output)])
    assert json.loads(output.read_text())["metadata"]["volume_provider"] == provider
    assert "| sequential-write |" in output.with_suffix(".md").read_text()
    assert run_chalk.call_count == (provider == "chalk")
    assert run_provider.call_count == (provider == "provider-new")
    archive.assert_not_called()


def test_chalk_default_preserves_ci_output_and_archive(tmp_path, monkeypatch):
    output = tmp_path / "ci.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))
    monkeypatch.setitem(PROVIDERS, "chalk", Provider(lambda *args, **kwargs: result()))
    archive = MagicMock()
    monkeypatch.setattr(harness, "archive_benchmark_results", archive)
    fio.cli([])
    archive.assert_called_once_with(output, "volume-fio")
    assert (
        json.loads(output.read_text())["benchmarks"][-1]["metadata"][
            "benchmark_error_count"
        ]
        == 0
    )


def test_new_provider_uses_registry_and_local_publication(tmp_path, monkeypatch):
    runner = MagicMock(return_value=result("provider-new"))
    monkeypatch.setitem(
        PROVIDERS,
        "provider-new",
        Provider(runner, supports_placement=True),
    )
    archive = MagicMock(side_effect=AssertionError("must not archive"))
    monkeypatch.setattr(harness, "archive_benchmark_results", archive)
    output = tmp_path / "new.json"
    fio.cli(
        [
            "--provider",
            "provider-new",
            "--provider-region",
            "test-region",
            "--output",
            str(output),
        ]
    )
    assert runner.call_args.kwargs["region"] == "test-region"
    assert (
        json.loads(output.read_text())["metadata"]["volume_provider"] == "provider-new"
    )
    archive.assert_not_called()


def test_buildkite_matrix_stays_chalk_only_and_fio_is_explicit():
    root = Path(__file__).parents[1]
    pipeline = yaml.safe_load((root / ".buildkite/sandbox-benchmark.yml").read_text())
    step = next(
        step
        for step in pipeline["steps"]
        if "Volume {{matrix.benchmark}}" in step["label"]
    )
    assert set(step["matrix"]["setup"]) == {"environment", "benchmark"}
    assert step["matrix"]["setup"]["benchmark"] == [
        "fio",
        "model-load",
        "model-download",
        "git-repo",
    ]
    if shutil.which("just") is None:
        pytest.skip("just is not installed")
    command = subprocess.run(
        [
            "just",
            "--justfile",
            str(root / "e2etests/volume/benchmarks/justfile"),
            "--dry-run",
            "fio",
            "staging/ftqa",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--provider chalk" in command.stderr + command.stdout


@pytest.mark.skipif(shutil.which("just") is None, reason="just is not installed")
def test_manual_recipe_preserves_arguments(tmp_path):
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if 'e2etests.volume.benchmarks.cases.fio.providers' in sys.argv:\n"
        "    print('provider-dependencies')\n"
        "else:\n"
        "    print(json.dumps(sys.argv[1:]))\n"
    )
    fake_uv.chmod(0o755)
    path = Path(__file__).parents[1] / "e2etests/volume/benchmarks/justfile"
    run = subprocess.run(
        [
            "just",
            "--justfile",
            str(path),
            "fio-run",
            "provider-a",
            "--output",
            "a b.json",
            "--size",
            "4M",
        ],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(run.stdout) == [
        "run",
        "--group",
        "provider-dependencies",
        "python",
        "-m",
        "e2etests.volume.benchmarks.cases.fio",
        "--output",
        "a b.json",
        "--size",
        "4M",
        "--provider",
        "provider-a",
        "--local",
    ]


@pytest.mark.parametrize(
    "workload_status,commit_status,expected",
    [(0, 0, 0), (2, 0, 2), (0, 75, 75), (2, 75, 2)],
)
def test_shared_entrypoint_preserves_failures_and_publishes_timing(
    tmp_path, workload_status, commit_status, expected
):
    worker = tmp_path / "worker.sh"
    worker.write_text(f"echo '{COMPLETION_MARKER}'\nexit {workload_status}\n")
    commit = tmp_path / "commit.sh"
    commit.write_text(f'echo commit >>"$1/commits"\nexit {commit_status}\n')
    date = tmp_path / "date"
    date.write_text("#!/bin/sh\necho 1000000000\n")
    date.chmod(0o755)
    run = subprocess.run(
        [
            "/bin/sh",
            "-c",
            ENTRYPOINT,
            "fio-benchmark",
            str(worker),
            str(tmp_path / "data"),
            str(tmp_path),
            str(commit),
        ],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        text=True,
        capture_output=True,
    )
    assert run.returncode == expected, run.stderr
    assert (tmp_path / "commits").read_text().splitlines() == ["commit", "commit"]
    assert json.loads((tmp_path / "data/results/volume-commit.json").read_text()) == {
        "elapsed_ns": 0
    }
