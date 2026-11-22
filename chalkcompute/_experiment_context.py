"""What a training function needs to take part in an experiment.

An experiment started with ``cc.Experiment.start(train=fn)`` runs ``fn`` as its
one training run. Inside it, ``cc.experiment_context()`` says which experiment
that is and hands the trained model back::

    @cc.training(dataset="support-triage", gpu="nvidia-l40s:1")
    def train_support_policy(df, config):
        ctx = cc.experiment_context()
        model = fine_tune(ctx.base_model, df, config)  # a PEFT model
        ctx.finish(model)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The training entrypoint puts the run's "experiment" config here before it calls the
# function. An environment variable reaches worker processes the function starts, too.
_EXPERIMENT_CONTEXT_ENV = "CHALK_EXPERIMENT_CONTEXT"
# The key of the training run's config under which the server describes the experiment.
EXPERIMENT_CONFIG_KEY = "experiment"
# Set on the policy server's container when it was started with a key.
_POLICY_SERVER_API_KEY_ENV = "POLICY_SERVER_API_KEY"


class ExperimentContextError(Exception):
    """Raised when a training run cannot take part in an experiment as asked."""


@dataclass(frozen=True)
class ExperimentContext:
    """The experiment a training run belongs to, and where its model goes."""

    experiment_id: str
    evaluation_id: str
    base_model: str
    """The model to train from, as given to ``Experiment.start``."""
    adapter_name: str
    """The name the trained adapter is saved and served under."""
    adapter_dir: str
    """The mount of the volume adapters are saved to."""
    policy_server_url: str
    """The model server the final evaluation run calls the trained model through."""
    policy_adapter_dir: str
    """Where the model server mounts the same volume."""
    baseline_run_id: str | None = None
    """The evaluation run the result is measured against, when one is pinned."""

    @property
    def adapter_path(self) -> Path:
        """Where :meth:`finish` saves the adapter."""
        return Path(self.adapter_dir) / self.adapter_name

    def finish(
        self,
        model: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
        load_timeout: float = 900.0,
    ) -> Path:
        """Hand the trained model to the experiment.

        Saves ``model``'s LoRA adapter, records it as the run's checkpoint, which
        the experiment registers a model version from, and loads it into the model
        server, which the final evaluation run calls. Call it once, when training
        is done.

        Parameters
        ----------
        model
            A PEFT model whose adapter was trained on ``base_model``.
        metadata
            Stored with the checkpoint, alongside the experiment and adapter names.
        load_timeout
            Seconds to keep asking the model server to load the adapter. Its mount
            of the adapter volume reloads on an interval, so the first attempts can
            fail before the saved files are visible to it.
        """
        path = self.adapter_path
        if path.exists():  # a retried run rewrites the adapter it already wrote
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(str(path))
        _commit_mount(self.adapter_dir)
        logger.info("Saved adapter to %s", path)

        _record_checkpoint(
            model,
            sorted(str(file) for file in path.iterdir() if file.is_file()),
            {
                **(metadata or {}),
                "experiment_id": self.experiment_id,
                "adapter_name": self.adapter_name,
                "base_model": self.base_model,
            },
        )
        _load_adapter(
            self.policy_server_url,
            self.adapter_name,
            f"{self.policy_adapter_dir.rstrip('/')}/{self.adapter_name}",
            load_timeout,
        )
        return path


def experiment_context() -> ExperimentContext:
    """The experiment the current training run belongs to.

    Raises :class:`ExperimentContextError` outside one: the function was run on
    its own, or by a server that predates experiments of this kind.
    """
    raw = os.environ.get(_EXPERIMENT_CONTEXT_ENV)
    if not raw:
        raise ExperimentContextError(
            "This training run is not part of an experiment. experiment_context() "
            "works in a function started with cc.Experiment.start(train=...)."
        )
    return _context_from_mapping(json.loads(raw))


def _publish_experiment_context(config: Mapping[str, Any] | None) -> None:
    """Make a training run's experiment, if it has one, available to the function."""
    experiment = None if config is None else config.get(EXPERIMENT_CONFIG_KEY)
    if isinstance(experiment, Mapping):
        os.environ[_EXPERIMENT_CONTEXT_ENV] = json.dumps(dict(experiment))


def _context_from_mapping(raw: Mapping[str, Any]) -> ExperimentContext:
    def required(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ExperimentContextError(
                f"The experiment did not say {key!r}; the server may be too old "
                "for this SDK."
            )
        return value

    baseline = raw.get("baseline_run_id")
    return ExperimentContext(
        experiment_id=required("experiment_id"),
        evaluation_id=required("evaluation_id"),
        base_model=required("base_model"),
        adapter_name=required("adapter_out"),
        adapter_dir=required("adapter_dir"),
        policy_server_url=required("policy_server_url"),
        policy_adapter_dir=required("policy_adapter_dir"),
        baseline_run_id=baseline if isinstance(baseline, str) and baseline else None,
    )


def _commit_mount(path: str) -> None:
    """Commit writes under a Chalk volume mount, so other mounts of it see them.

    Writes to a mounted volume stay local to the container until committed.
    Outside a Chalk mount there is nothing to commit.
    """
    control = Path(path) / ".chalk-volume" / "commit"
    if not control.exists():
        logger.info("%s is not a Chalk volume mount; nothing to commit", path)
        return
    subprocess.run([str(control)], check=True, timeout=600)


def _record_checkpoint(model: Any, files: list[str], metadata: dict[str, Any]) -> None:
    """Record the adapter as the training run's checkpoint.

    A checkpoint serializes a torch module's state dict, and handing it the PEFT
    model would upload the whole base model, so the module holds the LoRA weights
    alone. The adapter's own files go alongside.
    """
    try:
        import torch
        from chalk.ml.chalk_train import checkpoint
        from peft import (  # ty: ignore[import-not-found]
            get_peft_model_state_dict,
        )
    except ImportError as exc:
        raise ExperimentContextError(
            "finish() needs torch, peft and chalkpy[runtime] in the training image"
        ) from exc

    module = torch.nn.Module()
    for name, tensor in get_peft_model_state_dict(model).items():
        module.register_buffer(
            name.replace(".", "__"), tensor.detach().contiguous().cpu()
        )
    checkpoint(module, additional_files=files, metadata=metadata)


def _load_adapter(server_url: str, name: str, path: str, timeout: float) -> None:
    """Ask a vLLM server to load, or reload, a LoRA adapter, retrying until it can.

    vLLM answers 400 or 404 while the adapter's files are not yet visible in its
    own mount of the volume, and 5xx or a refused connection while it restarts,
    so those are retried until ``timeout``. A refused key never succeeds.
    """
    body = json.dumps(
        {"lora_name": name, "lora_path": path, "load_inplace": True}
    ).encode()
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(_POLICY_SERVER_API_KEY_ENV)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    deadline = time.monotonic() + timeout
    delay = 5.0
    while True:
        request = urllib.request.Request(
            f"{server_url.rstrip('/')}/v1/load_lora_adapter",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                logger.info("Model server loaded %s (%s)", name, response.status)
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            if exc.code in (401, 403):
                raise ExperimentContextError(
                    f"The model server refused the key: {detail}"
                ) from exc
            error = f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() + delay > deadline:
            raise ExperimentContextError(
                f"The model server did not load {name} from {path}: {error}"
            )
        time.sleep(delay)
        delay = min(delay * 2, 60.0)
