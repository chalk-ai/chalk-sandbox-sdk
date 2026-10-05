from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from chalkcompute import prime_rl
from chalkcompute._training import TrainingFunction, training
from chalkcompute.training import _prime_rl_metrics as metrics

TRAINER_LINE = (
    "22:17:13 SUCCESS Step 1 |   2m 1s | Loss 0.0071 | Entropy 0.6430 | Mismatch KL 0.0006 "
    "| Grad. Norm 0.9328 | LR 3.00e-06 | Throughput 519 tokens/s | MFU 0.7% | Peak Mem. 11.7 GiB"
)
ORCHESTRATOR_LINE = (
    "\x1b[32m22:16:36 SUCCESS\x1b[0m Step 1 |   26.0s | Reward 0.1738 | Trainable 112/128 (87.5%) "
    "| Turns 1.0 | Branches 1.0 | Max Off-Policy 0 | Error 0.0% | Truncation 75.9%"
)


def _run_forwarder(
    tmp_path: Path, rl_log: str, trainer_log: str
) -> list[tuple[int, dict[str, float]]]:
    (tmp_path / "rl.log").write_text(rl_log)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "trainer.log").write_text(trainer_log)
    (tmp_path / "done").touch()
    reports: list[tuple[int, dict[str, float]]] = []
    with patch.object(
        metrics, "report", lambda step, values: reports.append((step, values))
    ):
        metrics.main(tmp_path, tmp_path / "rl.log", tmp_path / "done")
    return reports


def test_forwarder_parses_both_log_shapes_and_reports_each_step_once(
    tmp_path: Path,
) -> None:
    # The trainer's line reaches rl.log with a process prefix and its own log without one.
    reports = _run_forwarder(
        tmp_path,
        rl_log=f"[trainer0]:{TRAINER_LINE}\n{ORCHESTRATOR_LINE}\n",
        trainer_log=f"{TRAINER_LINE}\n",
    )
    assert reports == [
        (
            1,
            {
                "loss": 0.0071,
                "entropy": 0.643,
                "grad_norm": 0.9328,
                "learning_rate": 3e-06,
            },
        ),
        (1, {"reward": 0.1738}),
    ]


def test_forwarder_ignores_lines_without_a_step(tmp_path: Path) -> None:
    reports = _run_forwarder(
        tmp_path,
        rl_log="WARNING Inference server was not reached | Error 1\n",
        trainer_log="",
    )
    assert reports == []


def test_forwarder_only_reads_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "partial.log"
    path.write_bytes(b"first\nsecond")
    offsets: dict[Path, int] = {}
    assert metrics.new_lines(path, offsets) == ["first"]
    path.write_bytes(b"first\nsecond\n")
    assert metrics.new_lines(path, offsets) == ["second"]


def _submit(**kwargs: Any) -> tuple[TrainingFunction, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_run_training(
        self: TrainingFunction, *, config: Any = None, **_: Any
    ) -> MagicMock:
        captured["fn"] = self
        captured["config"] = config
        return MagicMock()

    with (
        patch("chalkcompute._dataset.DatasetClient") as dataset_client,
        patch.object(TrainingFunction, "run_training", fake_run_training),
    ):
        prime_rl.train(**kwargs)
    captured["dataset_client"] = dataset_client
    return captured["fn"], captured


def test_train_submits_the_sdk_job_with_the_run_config() -> None:
    fn, captured = _submit(
        config="/app/configs/ci/integration/reverse_text/start.toml",
        gpu="nvidia-l4:2",
        max_steps=8,
        args=["--orchestrator.batch-size", "64"],
        env={"WANDB_MODE": "offline"},
        secrets=["HF_TOKEN"],
    )
    assert fn.func_name == "_train_in_container"
    assert fn.data_source == prime_rl.PLACEHOLDER_DATASET
    assert captured["config"] == {
        "gpus": 2,
        "config_path": "/app/configs/ci/integration/reverse_text/start.toml",
        "config_toml": None,
        "args": ["--max-steps", "8", "--orchestrator.batch-size", "64"],
    }
    assert fn._gpu == "nvidia-l4:2"
    assert fn._env["WANDB_MODE"] == "offline"
    assert fn._env["HF_HOME"] == "/tmp/huggingface"
    assert [secret._secret_name for secret in fn._secrets] == ["HF_TOKEN"]
    captured["dataset_client"].return_value.upload.assert_called_once_with(
        prime_rl.PLACEHOLDER_DATASET, {"placeholder": [0]}
    )


def test_train_uploads_a_local_config_inline_and_respects_an_explicit_dataset(
    tmp_path: Path,
) -> None:
    config = tmp_path / "run.toml"
    config.write_text("max_steps = 3\n")
    fn, captured = _submit(config=str(config), data="my-dataset")
    assert captured["config"]["config_toml"] == "max_steps = 3\n"
    assert fn.data_source == "my-dataset"
    captured["dataset_client"].return_value.upload.assert_not_called()


def test_image_installs_chalkpy_apart_and_environment_packages_into_prime_rl(
    tmp_path: Path,
) -> None:
    env_package = tmp_path / "dsgym_env"
    env_package.mkdir()
    (env_package / "pyproject.toml").write_text("[project]\nname = 'dsgym-env'\n")

    image = prime_rl.prime_rl_image(
        pip=["verifiers>=0.3"], env_packages=[str(env_package)]
    )

    commands = [
        command
        for step in image.to_proto().steps
        for command in getattr(step.step.value, "commands", [])
    ]
    assert "/opt/chalk-metrics" in commands[0] and prime_rl.CHALKPY in commands[0]
    assert (
        commands[1]
        == "uv pip install --no-config --python /app/.venv/bin/python /tmp/prime-rl-env-packages/dsgym_env"
    )
    assert (
        commands[2]
        == "uv pip install --no-config --python /app/.venv/bin/python 'verifiers>=0.3'"
    )
    assert not image.lazy_local_files, (
        "environment packages must be copied in so the install step sees them"
    )


def test_deploy_image_does_not_upload_sdk_source_for_sdk_training_functions() -> None:
    fn = training(prime_rl._train_in_container, data="ds")
    base = MagicMock()
    with patch("chalkcompute._function._make_base_image", return_value=base):
        assert fn._build_deploy_image() is base
    base.add_local_dir.assert_not_called()
    base.add_local_file.assert_not_called()


def test_cli_passes_through_arguments_after_double_dash() -> None:
    run = MagicMock(run_id="run-1")
    with (
        patch.object(prime_rl, "train", return_value=run) as train,
        patch.object(prime_rl, "dashboard_url", return_value="https://example/run-1"),
    ):
        status = prime_rl.main(
            [
                "train",
                "cfg.toml",
                "--name",
                "smoke",
                "--env",
                "A=b=c",
                "--detach",
                "--",
                "--max-steps",
                "8",
            ]
        )
    assert status == 0
    kwargs = train.call_args.kwargs
    assert train.call_args.args == ("cfg.toml",)
    assert kwargs["name"] == "smoke"
    assert kwargs["env"] == {"A": "b=c"}
    assert kwargs["args"] == ["--max-steps", "8"]
