"""Experiments: train a model on an evaluation and judge it with the same evaluation.

An experiment post-trains ``base_model`` on an evaluation's task, using the
evaluation's scorers as the reward. It pins a baseline run of the evaluation
when it starts, registers the trained model, and scores that model with one
final run of the same evaluation, so the two runs compare like for like.

The evaluation is referenced, not owned: create it with
:meth:`Evaluation.create` and reuse it across experiments.
"""

from __future__ import annotations

import builtins
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf.wkt import field_mask_pb, struct_pb

from chalkcompute._connect import ConnectClient
from chalkcompute._containerspec import ComputeClass
from chalkcompute._evaluation import (
    Evaluation,
    EvaluationClient,
    EvaluationRun,
    _http_status_for_code,
)
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.evaluation.v1 import (
    experiment_connect,
    experiment_pb,
)
from chalkcompute._tracing import _instrumented
from chalkcompute._training import TrainingRunHandle


class ExperimentError(Exception):
    """Base exception for experiment SDK operations."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ExperimentNotFoundError(ExperimentError):
    """Raised when an experiment does not exist."""


class ExperimentStatus(str, Enum):
    """Lifecycle state of an experiment."""

    UNSPECIFIED = "unspecified"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ExperimentPhase(str, Enum):
    """What an experiment's training step is doing."""

    UNSPECIFIED = "unspecified"
    ROLLOUT = "rollout"
    """The current model's evaluation runs are executing."""
    TRAINING = "training"
    """The training run is taking a step on the scored rollouts."""
    DONE = "done"


_TERMINAL_STATUSES = frozenset(
    {
        ExperimentStatus.SUCCEEDED,
        ExperimentStatus.FAILED,
        ExperimentStatus.CANCELED,
    }
)


@dataclass(frozen=True)
class ExperimentStep:
    """One rollout-and-train step of an experiment."""

    index: int
    phase: ExperimentPhase
    policy_model: str
    """The model name this step's evaluation runs called."""
    evaluation_run_ids: tuple[str, ...]
    training_run_id: str | None
    mean_reward: float | None
    """Mean weighted reward over this step's rollouts."""
    scorer_scores: dict[str, float]
    """Each scorer's average score over this step's rollouts, by scorer id."""


@dataclass(frozen=True)
class ExperimentScorerResult:
    """One scorer's score before and after training."""

    scorer_id: str
    baseline: float | None
    trained: float | None

    @property
    def change(self) -> float | None:
        """``trained - baseline``, when both runs were scored."""
        if self.baseline is None or self.trained is None:
            return None
        return self.trained - self.baseline


@dataclass(frozen=True)
class ExperimentResult:
    """The per-scorer comparison of the final run against the pinned baseline."""

    scorers: tuple[ExperimentScorerResult, ...]

    def table(self) -> str:
        """Render ``scorer | baseline | trained | change`` as aligned text."""
        rows = [("scorer", "baseline", "trained", "change")]
        for scorer in self.scorers:
            rows.append(
                (
                    scorer.scorer_id,
                    _format_score(scorer.baseline),
                    _format_score(scorer.trained),
                    _format_score(scorer.change, signed=True),
                )
            )
        widths = [max(len(row[column]) for row in rows) for column in range(4)]
        return "\n".join(
            " | ".join(
                cell.ljust(widths[column])
                if column == 0
                else cell.rjust(widths[column])
                for column, cell in enumerate(row)
            )
            for row in rows
        )


@dataclass
class Experiment:
    """A training of one model on one evaluation, and its before/after result."""

    id: str
    evaluation_id: str
    status: ExperimentStatus
    base_model: str
    register_as: str | None = None
    """The registry model the trained model is registered as a version of."""
    baseline_run_id: str | None = None
    """The evaluation run the result is measured against, pinned at start."""
    training_run_id: str | None = None
    model_version_id: str | None = None
    final_run_id: str | None = None
    """The evaluation run of the trained model on the same dataset."""
    steps: tuple[ExperimentStep, ...] = ()
    result: ExperimentResult = field(default_factory=lambda: ExperimentResult(()))
    error_message: str | None = None
    retry_count: int = 0
    created_by: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    _client: ExperimentClient | None = field(default=None, repr=False, compare=False)

    @classmethod
    def start(
        cls,
        evaluation: Evaluation | str,
        *,
        base_model: str,
        gpu: str,
        steps: int,
        samples_per_row: int,
        reward_weights: Mapping[str, float] | None = None,
        learning_rate: float | None = None,
        lora_rank: int | None = None,
        register_as: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        trainer_image: str | None = None,
        compute_class: ComputeClass | None = None,
        chalk_client: Any | None = None,
    ) -> Experiment:
        """Start an experiment on an existing evaluation.

        Parameters
        ----------
        evaluation
            The evaluation that judges the experiment, or its id. Its dataset,
            task and scorers stay fixed for the experiment's life.
        base_model
            Hugging Face id of the model training starts from.
        gpu
            GPU request for training, e.g. ``"nvidia-l40s:1"``.
        steps
            Rollout-and-train steps to take.
        samples_per_row
            Evaluation runs per step; each dataset row gets this many samples.
        reward_weights
            Scorer id to reward weight; a scorer to minimize takes a negative
            weight. Defaults to every scorer of the evaluation at ``1.0``.
        learning_rate, lora_rank
            Left unset, the server chooses.
        register_as
            Registry model to register the trained model under. Defaults to a
            name derived from the evaluation's.
        recipe
            Settings passed unchanged to the trainer image, which validates them.
        trainer_image
            Image the training runs execute. Defaults to the server's trainer.
        compute_class
            Where training runs execute. Defaults to Kubernetes.
        """
        return ExperimentClient(chalk_client=chalk_client).start(
            evaluation,
            base_model=base_model,
            gpu=gpu,
            steps=steps,
            samples_per_row=samples_per_row,
            reward_weights=reward_weights,
            learning_rate=learning_rate,
            lora_rank=lora_rank,
            register_as=register_as,
            recipe=recipe,
            trainer_image=trainer_image,
            compute_class=compute_class,
        )

    @classmethod
    def from_id(
        cls, experiment_id: str, *, chalk_client: Any | None = None
    ) -> Experiment:
        """Attach to an existing experiment by identifier."""
        return ExperimentClient(chalk_client=chalk_client).get(experiment_id)

    @classmethod
    def list_all(
        cls,
        *,
        evaluation: Evaluation | str | None = None,
        chalk_client: Any | None = None,
    ) -> list[Experiment]:
        """List the environment's experiments, newest first.

        ``evaluation`` keeps only the experiments it judged.
        """
        return ExperimentClient(chalk_client=chalk_client).list(evaluation=evaluation)

    @property
    def is_terminal(self) -> bool:
        """Whether this experiment has reached a terminal state."""
        return self.status in _TERMINAL_STATUSES

    @property
    def evaluation(self) -> Evaluation:
        """The evaluation that judges this experiment."""
        return self._require_client().evaluations.get(self.evaluation_id)

    @property
    def baseline_run(self) -> EvaluationRun | None:
        """The pinned baseline run, once the experiment has one."""
        if self.baseline_run_id is None:
            return None
        return self._require_client().evaluations.get_run(self.baseline_run_id)

    @property
    def final_run(self) -> EvaluationRun | None:
        """The trained model's run of the evaluation, once it exists."""
        if self.final_run_id is None:
            return None
        return self._require_client().evaluations.get_run(self.final_run_id)

    @property
    def training_run(self) -> TrainingRunHandle | None:
        """The training run whose checkpoint is the experiment's model."""
        if self.training_run_id is None:
            return None
        return TrainingRunHandle(self.training_run_id, self._require_client()._connect)

    def refresh(self) -> Experiment:
        """Return the latest server representation of this experiment."""
        return self._require_client().get(self.id)

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 30.0,
        raise_on_failure: bool = True,
    ) -> Experiment:
        """Wait for this experiment to finish and return its final representation.

        Parameters
        ----------
        timeout
            Maximum seconds to wait. ``None``, the default, waits indefinitely:
            an experiment trains on a GPU and can run for hours.
        poll_interval
            Seconds between status polls.
        raise_on_failure
            Raise :class:`ExperimentFailedError` for a failed or canceled
            experiment. Set to ``False`` to inspect the terminal handle directly.
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0 or None")
        if poll_interval < 0:
            raise ValueError("poll_interval must be >= 0")

        deadline = None if timeout is None else time.monotonic() + timeout
        current = self
        while not current.is_terminal:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Experiment {self.id!r} did not finish within {timeout}s"
                    )
                time.sleep(min(poll_interval, remaining))
            else:
                time.sleep(poll_interval)
            current = current.refresh()

        if raise_on_failure and current.status is not ExperimentStatus.SUCCEEDED:
            raise ExperimentFailedError(current)
        return current

    def cancel(self) -> Experiment:
        """Cancel this experiment and its in-flight training run."""
        return self._require_client().cancel(self.id)

    def retry(
        self,
        *,
        gpu: str | None = None,
        steps: int | None = None,
        learning_rate: float | None = None,
        register_as: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        trainer_image: str | None = None,
        compute_class: ComputeClass | None = None,
    ) -> Experiment:
        """Resume a failed or canceled experiment at its latest training step.

        The step's completed rollout is reused. An argument replaces that
        setting before retrying; ``steps`` may only grow. Settings the rollouts
        were generated under, such as ``base_model``, cannot change.
        """
        return self._require_client().retry(
            self.id,
            gpu=gpu,
            steps=steps,
            learning_rate=learning_rate,
            register_as=register_as,
            recipe=recipe,
            trainer_image=trainer_image,
            compute_class=compute_class,
        )

    def _require_client(self) -> ExperimentClient:
        if self._client is None:
            raise ExperimentError(
                "Experiment has no attached client. Fetch or start it through "
                "ExperimentClient."
            )
        return self._client


class ExperimentFailedError(ExperimentError):
    """Raised when a waited experiment fails or is canceled."""

    def __init__(self, experiment: Experiment) -> None:
        detail = (
            experiment.error_message
            or f"experiment entered {experiment.status.value} state"
        )
        super().__init__(f"Experiment {experiment.id!r} did not succeed: {detail}")
        self.experiment = experiment


class ExperimentClient:
    """Client for starting experiments and following them to a result."""

    def __init__(
        self,
        *,
        chalk_client: Any | None = None,
        connect_client: ConnectClient | None = None,
    ) -> None:
        if chalk_client is not None and connect_client is not None:
            raise ValueError("pass only one of chalk_client or connect_client")
        self._connect = connect_client or ConnectClient(chalk_client=chalk_client)
        self.evaluations = EvaluationClient(connect_client=self._connect)

    @_instrumented("chalkcompute.experiment.start")
    def start(
        self,
        evaluation: Evaluation | str,
        *,
        base_model: str,
        gpu: str,
        steps: int,
        samples_per_row: int,
        reward_weights: Mapping[str, float] | None = None,
        learning_rate: float | None = None,
        lora_rank: int | None = None,
        register_as: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        trainer_image: str | None = None,
        compute_class: ComputeClass | None = None,
    ) -> Experiment:
        """Start an experiment on an existing evaluation.

        See :meth:`Experiment.start` for the parameters.
        """
        evaluation_id = _evaluation_id(evaluation)
        base_model = _required(base_model, name="base_model")
        gpu = _required(gpu, name="gpu")
        if reward_weights is None:
            definition = (
                evaluation
                if isinstance(evaluation, Evaluation)
                else self.evaluations.get(evaluation_id)
            )
            reward_weights = {scorer.id: 1.0 for scorer in definition.scorers}
        config = experiment_pb.ExperimentConfig(
            base_model=base_model,
            iterations=steps,
            samples_per_row=samples_per_row,
            reward_weights=_reward_weights(reward_weights),
            learning_rate=0.0 if learning_rate is None else learning_rate,
            lora_rank=0 if lora_rank is None else lora_rank,
            trainer_image="" if trainer_image is None else trainer_image.strip(),
            trainer_gpu=gpu,
            model_registry_name=_optional(register_as, name="register_as"),
            trainer_compute_class=_compute_class_to_proto(compute_class),
            recipe=_recipe_struct(recipe),
        )
        stub = self._connect.stub(experiment_connect.ExperimentServiceClientSync)
        response = self._call(
            stub.start_experiment,
            experiment_pb.StartExperimentRequest(
                evaluation_id=evaluation_id, config=config
            ),
            not_found_subject=f"Evaluation not found: {evaluation_id}",
        )
        if response.experiment is None:
            raise ExperimentError("StartExperiment returned no experiment")
        return _experiment_from_proto(response.experiment, self)

    @_instrumented("chalkcompute.experiment.get")
    def get(self, experiment_id: str) -> Experiment:
        """Fetch an experiment by identifier."""
        experiment_id = _required(experiment_id, name="experiment_id")
        stub = self._connect.stub(experiment_connect.ExperimentServiceClientSync)
        response = self._call(
            stub.get_experiment,
            experiment_pb.GetExperimentRequest(experiment_id=experiment_id),
            not_found_subject=f"Experiment not found: {experiment_id}",
        )
        if response.experiment is None:
            raise ExperimentError("GetExperiment returned no experiment")
        return _experiment_from_proto(response.experiment, self)

    @_instrumented("chalkcompute.experiment.list")
    def list(
        self, *, evaluation: Evaluation | str | None = None
    ) -> builtins.list[Experiment]:
        """List the environment's experiments, newest first.

        ``evaluation`` keeps only the experiments it judged.
        """
        filters = experiment_pb.ListExperimentsFilters(
            evaluation_id=None if evaluation is None else _evaluation_id(evaluation)
        )
        stub = self._connect.stub(experiment_connect.ExperimentServiceClientSync)
        response = self._call(
            stub.list_experiments,
            experiment_pb.ListExperimentsRequest(filters=filters),
        )
        return [_experiment_from_proto(raw, self) for raw in response.experiments]

    @_instrumented("chalkcompute.experiment.cancel")
    def cancel(self, experiment_id: str) -> Experiment:
        """Cancel an experiment and its in-flight training run."""
        experiment_id = _required(experiment_id, name="experiment_id")
        stub = self._connect.stub(experiment_connect.ExperimentServiceClientSync)
        response = self._call(
            stub.cancel_experiment,
            experiment_pb.CancelExperimentRequest(experiment_id=experiment_id),
            not_found_subject=f"Experiment not found: {experiment_id}",
        )
        if response.experiment is None:
            raise ExperimentError("CancelExperiment returned no experiment")
        return _experiment_from_proto(response.experiment, self)

    @_instrumented("chalkcompute.experiment.retry")
    def retry(
        self,
        experiment_id: str,
        *,
        gpu: str | None = None,
        steps: int | None = None,
        learning_rate: float | None = None,
        register_as: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        trainer_image: str | None = None,
        compute_class: ComputeClass | None = None,
    ) -> Experiment:
        """Resume a failed or canceled experiment at its latest training step.

        See :meth:`Experiment.retry` for the parameters.
        """
        experiment_id = _required(experiment_id, name="experiment_id")
        # Keyed by ExperimentConfig field name: the keys are the update mask.
        replaced: dict[str, Any] = {}
        if gpu is not None:
            replaced["trainer_gpu"] = _required(gpu, name="gpu")
        if steps is not None:
            replaced["iterations"] = steps
        if learning_rate is not None:
            replaced["learning_rate"] = learning_rate
        if register_as is not None:
            replaced["model_registry_name"] = _required(register_as, name="register_as")
        if recipe is not None:
            replaced["recipe"] = _recipe_struct(recipe)
        if trainer_image is not None:
            replaced["trainer_image"] = _required(trainer_image, name="trainer_image")
        if compute_class is not None:
            replaced["trainer_compute_class"] = _compute_class_to_proto(compute_class)
        stub = self._connect.stub(experiment_connect.ExperimentServiceClientSync)
        response = self._call(
            stub.retry_experiment,
            experiment_pb.RetryExperimentRequest(
                experiment_id=experiment_id,
                config=experiment_pb.ExperimentConfig(**replaced) if replaced else None,
                update_mask=(
                    field_mask_pb.FieldMask(paths=[*replaced]) if replaced else None
                ),
            ),
            not_found_subject=f"Experiment not found: {experiment_id}",
        )
        if response.experiment is None:
            raise ExperimentError("RetryExperiment returned no experiment")
        return _experiment_from_proto(response.experiment, self)

    def _call(
        self,
        method: Any,
        request: Any,
        *,
        not_found_subject: str | None = None,
    ) -> Any:
        try:
            return self._connect.call(method, request)
        except ConnectRpcError as exc:
            status_code = _http_status_for_code(exc.code)
            if exc.code == Code.NOT_FOUND and not_found_subject is not None:
                if str(exc):
                    not_found_subject = f"{not_found_subject}: {exc}"
                raise ExperimentNotFoundError(
                    not_found_subject, status_code=status_code
                ) from exc
            raise ExperimentError(
                f"{getattr(method, '__name__', 'experiment RPC')} failed: {exc}",
                status_code=status_code,
            ) from exc


def _evaluation_id(evaluation: Evaluation | str) -> str:
    if isinstance(evaluation, Evaluation):
        return evaluation.id
    if not isinstance(evaluation, str):
        raise TypeError("evaluation must be an Evaluation or an evaluation id")
    return _required(evaluation, name="evaluation")


def _required(value: str, *, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _optional(value: str | None, *, name: str) -> str | None:
    return None if value is None else _required(value, name=name)


def _reward_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if not weights:
        raise ValueError("reward_weights must weight at least one scorer")
    parsed: dict[str, float] = {}
    for scorer_id, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise TypeError(f"reward_weights[{scorer_id!r}] must be a number")
        if not math.isfinite(weight):
            raise ValueError(f"reward_weights[{scorer_id!r}] must be finite")
        parsed[scorer_id] = float(weight)
    return parsed


def _recipe_struct(recipe: Mapping[str, Any] | None) -> struct_pb.Struct | None:
    if recipe is None:
        return None
    try:
        encoded = json.dumps(dict(recipe), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("recipe must be JSON serializable") from exc
    return struct_pb.Struct.from_json(encoded)


def _compute_class_to_proto(
    compute_class: ComputeClass | None,
) -> container_pb.ComputeClass | None:
    if compute_class is None:
        return None
    return container_pb.ComputeClass[compute_class.name]


def _experiment_from_proto(
    raw: experiment_pb.Experiment, client: ExperimentClient
) -> Experiment:
    config = raw.config
    baseline = dict(raw.baseline_scorer_scores)
    trained = dict(raw.final_scorer_scores)
    weights = {} if config is None else dict(config.reward_weights)
    # Weighted scorers first, in the order the config lists them, then the rest.
    scorer_ids = list(dict.fromkeys([*weights, *sorted({*baseline, *trained})]))
    return Experiment(
        id=raw.id,
        evaluation_id=raw.evaluation_id,
        status=_parse_status(raw.status),
        base_model="" if config is None else config.base_model,
        register_as=None if config is None else config.model_registry_name or None,
        baseline_run_id=raw.baseline_run_id or None,
        training_run_id=raw.training_run_id or None,
        model_version_id=raw.model_version_id or None,
        final_run_id=raw.final_run_id or None,
        steps=tuple(_step_from_proto(step) for step in raw.iterations),
        result=ExperimentResult(
            tuple(
                ExperimentScorerResult(
                    scorer_id=scorer_id,
                    baseline=baseline.get(scorer_id),
                    trained=trained.get(scorer_id),
                )
                for scorer_id in scorer_ids
            )
        ),
        error_message=raw.error_message or None,
        retry_count=raw.retry_count,
        created_by=raw.created_by or None,
        created_at=None if raw.created_at is None else raw.created_at.to_datetime(),
        started_at=None if raw.started_at is None else raw.started_at.to_datetime(),
        completed_at=(
            None if raw.completed_at is None else raw.completed_at.to_datetime()
        ),
        _client=client,
    )


def _step_from_proto(raw: experiment_pb.ExperimentIteration) -> ExperimentStep:
    return ExperimentStep(
        index=raw.iteration,
        phase=_parse_phase(raw.phase),
        policy_model=raw.policy_model,
        evaluation_run_ids=tuple(raw.evaluation_run_ids),
        training_run_id=raw.training_run_id or None,
        mean_reward=raw.mean_reward if raw.has_field("mean_reward") else None,
        scorer_scores=dict(raw.scorer_average_scores),
    )


def _parse_status(value: experiment_pb.ExperimentStatus) -> ExperimentStatus:
    try:
        return ExperimentStatus(value.name.lower())
    except (AttributeError, ValueError) as exc:
        raise ExperimentError(f"unknown experiment status: {value}") from exc


def _parse_phase(value: experiment_pb.ExperimentPhase) -> ExperimentPhase:
    # A phase added on the server after this SDK was built reads as unspecified
    # rather than failing the whole experiment lookup.
    try:
        return ExperimentPhase(value.name.lower())
    except (AttributeError, ValueError):
        return ExperimentPhase.UNSPECIFIED


def _format_score(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:+.3f}" if signed else f"{value:.3f}"
