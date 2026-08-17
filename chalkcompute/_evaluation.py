"""Versioned evaluation definitions and runs.

Evaluations bind a completed Chalk dataset revision to one deployed task
function version and one or more deployed scorer function versions.  Creating
an evaluation persists that immutable definition; :meth:`Evaluation.run`
queues a separate execution of it.

The task and scorers may be handles returned directly by
``@chalkcompute.function`` or references created with
``RemoteFunction.from_name`` / ``RemoteFunction.from_id``.  In either case,
the handle must already expose a function version.  Evaluation creation never
silently deploys an imperative, undeployed ``RemoteFunction``.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
from chalkcompute._connect import ConnectClient
from chalkcompute._function import DeployedFunction, RemoteFunction
from chalkcompute._tracing import _instrumented

_EVALUATION_SERVICE = "chalk.evaluation.v1.EvaluationService"


class EvaluationError(Exception):
    """Base exception for evaluation SDK operations."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EvaluationNotFoundError(EvaluationError):
    """Raised when an evaluation or evaluation run does not exist."""


def _scorer_result_pyarrow_type() -> Any:
    import pyarrow as pa

    return pa.struct(
        [
            pa.field("name", pa.large_utf8(), nullable=False),
            pa.field("score", pa.float64(), nullable=False),
            pa.field("metadata", pa.large_utf8(), nullable=True),
        ]
    )


@dataclass(frozen=True)
class EvaluationScorerResult:
    """The score and optional row-level metadata returned by a scorer function.

    Annotating a ``@chalkcompute.function`` with this return type installs its
    Arrow struct contract automatically. Scorer functions must return an
    ``EvaluationScorerResult`` instance rather than an untyped dictionary.
    """

    name: str
    """Non-empty metric name used to distinguish scores from the same scorer."""

    score: float
    """Normalized score in the inclusive range ``[0, 1]``."""

    metadata: Mapping[str, Any] | None = None
    """Optional JSON-serializable metadata retained with the scored row."""

    __chalk_arrow_type__ = arrow_pb2.ArrowType(
        struct=arrow_pb2.Struct(
            sub_field_types=[
                arrow_pb2.Field(
                    name="name",
                    arrow_type=arrow_pb2.ArrowType(large_utf8=arrow_pb2.EmptyMessage()),
                    nullable=False,
                ),
                arrow_pb2.Field(
                    name="score",
                    arrow_type=arrow_pb2.ArrowType(float64=arrow_pb2.EmptyMessage()),
                    nullable=False,
                ),
                arrow_pb2.Field(
                    name="metadata",
                    arrow_type=arrow_pb2.ArrowType(large_utf8=arrow_pb2.EmptyMessage()),
                    nullable=True,
                ),
            ]
        )
    )
    __chalk_pyarrow_type__ = staticmethod(_scorer_result_pyarrow_type)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("name must be a string")
        name = self.name.strip()
        if not name:
            raise ValueError("name is required")
        object.__setattr__(self, "name", name)

        if isinstance(self.score, bool):
            raise TypeError("score must be a number")
        try:
            score = float(self.score)
        except (TypeError, ValueError) as exc:
            raise TypeError("score must be a number") from exc
        if not math.isfinite(score):
            raise ValueError("score must be finite")
        if not 0 <= score <= 1:
            raise ValueError("score must be between 0 and 1")
        object.__setattr__(self, "score", score)

        if self.metadata is not None:
            metadata = _metadata_dict(self.metadata)
            try:
                json.dumps(metadata, allow_nan=False)
            except ValueError as exc:
                raise TypeError("metadata must be JSON-serializable") from exc
            object.__setattr__(self, "metadata", metadata)

    def __chalk_serialize__(self) -> Any:
        """Encode this result as the Arrow struct registered in the catalog."""
        import pyarrow as pa

        metadata = None
        if self.metadata is not None:
            metadata = json.dumps(
                self.metadata,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        return pa.scalar(
            {"name": self.name, "score": self.score, "metadata": metadata},
            type=_scorer_result_pyarrow_type(),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> EvaluationScorerResult:
        """Decode an Arrow scorer-result struct returned by Chalk Compute."""
        raw = value.as_py()
        metadata = raw.get("metadata")
        return cls(
            name=raw["name"],
            score=raw["score"],
            metadata=None if metadata is None else json.loads(metadata),
        )


class DatasetRevisionLike(Protocol):
    """Structural type accepted for ChalkPy dataset revisions."""

    @property
    def dataset_id(self) -> object:
        """Dataset identifier exposed by the revision handle."""
        ...

    @property
    def revision_id(self) -> object:
        """Revision identifier exposed by the revision handle."""
        ...


class DatasetNameLike(Protocol):
    """Structural type accepted for references to the latest dataset revision."""

    @property
    def dataset_name(self) -> object:
        """Dataset name whose latest revision should be resolved."""
        ...


@dataclass(frozen=True)
class DatasetRevisionRef:
    """A reference to a specific revision or a dataset's latest revision."""

    dataset_id: str | None = None
    """Dataset identifier. Must be paired with ``revision_id``."""

    revision_id: str | None = None
    """Completed dataset revision identifier. Must be paired with ``dataset_id``."""

    dataset_name: str | None = None
    """Dataset whose latest revision should be resolved when creating an evaluation."""

    def __post_init__(self) -> None:
        dataset_id = self.dataset_id.strip() if self.dataset_id is not None else None
        revision_id = self.revision_id.strip() if self.revision_id is not None else None
        dataset_name = (
            self.dataset_name.strip() if self.dataset_name is not None else None
        )
        if dataset_name is not None:
            if not dataset_name:
                raise ValueError("dataset_name is required")
            if dataset_id is not None or revision_id is not None:
                raise ValueError(
                    "pass either dataset_name or dataset_id and revision_id, not both"
                )
            object.__setattr__(self, "dataset_name", dataset_name)
            return
        if not dataset_id:
            raise ValueError("dataset_id is required")
        if not revision_id:
            raise ValueError("revision_id is required")
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "revision_id", revision_id)


@dataclass(frozen=True)
class EvaluationScorer:
    """A versioned scorer attached to an evaluation."""

    id: str
    """Evaluation-local scorer identifier."""

    function_version_id: str | None = None
    """Remote function version backing the scorer, when function-backed."""

    builtin_id: str | None = None
    """Immutable built-in scorer identifier, when built-in."""


class EvaluationRunStatus(str, Enum):
    """Lifecycle state of an evaluation run."""

    UNSPECIFIED = "unspecified"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


_TERMINAL_RUN_STATUSES = frozenset(
    {
        EvaluationRunStatus.SUCCEEDED,
        EvaluationRunStatus.FAILED,
        EvaluationRunStatus.CANCELED,
    }
)


@dataclass
class Evaluation:
    """A persisted, immutable evaluation definition."""

    id: str
    name: str
    dataset: DatasetRevisionRef
    task_function_version_id: str
    scorers: tuple[EvaluationScorer, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    team_id: str | None = None
    environment_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    _client: EvaluationClient | None = field(default=None, repr=False, compare=False)

    @property
    def dataset_id(self) -> str:
        """Dataset identifier pinned by this evaluation."""
        if self.dataset.dataset_id is None:
            raise EvaluationError("evaluation response did not include a dataset_id")
        return self.dataset.dataset_id

    @property
    def dataset_revision_id(self) -> str:
        """Dataset revision identifier pinned by this evaluation."""
        if self.dataset.revision_id is None:
            raise EvaluationError(
                "evaluation response did not include a dataset_revision_id"
            )
        return self.dataset.revision_id

    @classmethod
    def create(
        cls,
        name: str,
        *,
        dataset: DatasetRevisionRef | DatasetRevisionLike | DatasetNameLike,
        task: DeployedFunction,
        scorers: Sequence[DeployedFunction],
        metadata: Mapping[str, Any] | None = None,
        chalk_client: Any | None = None,
    ) -> Evaluation:
        """Create an evaluation from deployed function handles.

        ``task`` and every scorer must already be deployed.  Handles returned
        by ``@chalkcompute.function`` meet this requirement because the
        decorator deploys synchronously before returning.
        """
        return EvaluationClient(chalk_client=chalk_client).create(
            name,
            dataset=dataset,
            task=task,
            scorers=scorers,
            metadata=metadata,
        )

    @classmethod
    def from_id(
        cls, evaluation_id: str, *, chalk_client: Any | None = None
    ) -> Evaluation:
        """Attach to an existing evaluation by identifier."""
        return EvaluationClient(chalk_client=chalk_client).get(evaluation_id)

    def refresh(self) -> Evaluation:
        """Return the latest server representation of this evaluation."""
        return self._require_client().get(self.id)

    def run(self, *, metadata: Mapping[str, Any] | None = None) -> EvaluationRun:
        """Queue a run of this evaluation and return its handle."""
        return self._require_client().run(self.id, metadata=metadata)

    def runs(self, *, limit: int | None = None) -> Iterator[EvaluationRun]:
        """Iterate this evaluation's runs, newest first."""
        return self._require_client().list_runs(evaluation_id=self.id, limit=limit)

    def _require_client(self) -> EvaluationClient:
        if self._client is None:
            raise EvaluationError(
                "Evaluation has no attached client. Fetch or create it through "
                "EvaluationClient."
            )
        return self._client


@dataclass
class EvaluationRun:
    """A queued or completed execution of an evaluation."""

    id: str
    evaluation_id: str
    status: EvaluationRunStatus
    metadata: dict[str, Any] = field(default_factory=dict)
    team_id: str | None = None
    environment_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_dataset_id: str | None = None
    result_dataset_revision_id: str | None = None
    error_message: str | None = None
    _client: EvaluationClient | None = field(default=None, repr=False, compare=False)

    @property
    def is_terminal(self) -> bool:
        """Whether this run has reached a terminal state."""
        return self.status in _TERMINAL_RUN_STATUSES

    @property
    def result_dataset(self) -> DatasetRevisionRef | None:
        """Result dataset revision produced by a successful run, if available."""
        if self.result_dataset_id and self.result_dataset_revision_id:
            return DatasetRevisionRef(
                dataset_id=self.result_dataset_id,
                revision_id=self.result_dataset_revision_id,
            )
        return None

    @classmethod
    def from_id(
        cls, evaluation_run_id: str, *, chalk_client: Any | None = None
    ) -> EvaluationRun:
        """Attach to an existing evaluation run by identifier."""
        return EvaluationClient(chalk_client=chalk_client).get_run(evaluation_run_id)

    def refresh(self) -> EvaluationRun:
        """Return the latest server representation of this run."""
        return self._require_client().get_run(self.id)

    def wait(
        self,
        *,
        timeout: float | None = 600.0,
        poll_interval: float = 2.0,
        raise_on_failure: bool = True,
    ) -> EvaluationRun:
        """Wait for this run to finish and return its final representation.

        Parameters
        ----------
        timeout
            Maximum seconds to wait. ``None`` waits indefinitely.
        poll_interval
            Seconds between status polls.
        raise_on_failure
            Raise :class:`EvaluationRunFailedError` for failed or canceled
            runs.  Set to ``False`` to inspect the terminal handle directly.
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
                        f"Evaluation run {self.id!r} did not finish within {timeout}s"
                    )
                time.sleep(min(poll_interval, remaining))
            else:
                time.sleep(poll_interval)
            current = current.refresh()

        if raise_on_failure and current.status in {
            EvaluationRunStatus.FAILED,
            EvaluationRunStatus.CANCELED,
        }:
            raise EvaluationRunFailedError(current)
        return current

    def _require_client(self) -> EvaluationClient:
        if self._client is None:
            raise EvaluationError(
                "EvaluationRun has no attached client. Fetch or create it through "
                "EvaluationClient."
            )
        return self._client


class EvaluationRunFailedError(EvaluationError):
    """Raised when a waited evaluation run fails or is canceled."""

    def __init__(self, run: EvaluationRun) -> None:
        detail = run.error_message or f"run entered {run.status.value} state"
        super().__init__(f"Evaluation run {run.id!r} did not succeed: {detail}")
        self.run = run


class EvaluationClient:
    """Client for creating evaluation definitions and managing their runs."""

    def __init__(
        self,
        *,
        chalk_client: Any | None = None,
        connect_client: ConnectClient | None = None,
    ) -> None:
        if chalk_client is not None and connect_client is not None:
            raise ValueError("pass only one of chalk_client or connect_client")
        self._connect = connect_client or ConnectClient(chalk_client=chalk_client)

    @_instrumented("chalkcompute.evaluation.create")
    def create(
        self,
        name: str,
        *,
        dataset: DatasetRevisionRef | DatasetRevisionLike | DatasetNameLike,
        task: DeployedFunction,
        scorers: Sequence[DeployedFunction],
        metadata: Mapping[str, Any] | None = None,
    ) -> Evaluation:
        """Persist an evaluation pinned to deployed function versions."""
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        dataset_ref = _resolve_dataset_revision(dataset)
        task_version_id = _resolve_function_version(task, role="task")
        scorer_handles = tuple(scorers)
        if not scorer_handles:
            raise ValueError("at least one scorer is required")
        scorer_version_ids = [
            _resolve_function_version(scorer, role=f"scorer {index}")
            for index, scorer in enumerate(scorer_handles)
        ]
        metadata_dict = _metadata_dict(metadata)

        request: dict[str, Any] = {
            "name": name,
            "taskFunctionVersionId": task_version_id,
            "scorers": [
                {"function": {"functionVersionId": version_id}}
                for version_id in scorer_version_ids
            ],
        }
        if dataset_ref.dataset_name is not None:
            request["datasetName"] = dataset_ref.dataset_name
        else:
            request["datasetId"] = dataset_ref.dataset_id
            request["datasetRevisionId"] = dataset_ref.revision_id
        if metadata is not None:
            request["metadata"] = metadata_dict
        response = self._rpc("CreateEvaluation", request)
        return _evaluation_from_response(response, self)

    @_instrumented("chalkcompute.evaluation.get")
    def get(self, evaluation_id: str) -> Evaluation:
        """Fetch an evaluation by identifier."""
        evaluation_id = evaluation_id.strip()
        if not evaluation_id:
            raise ValueError("evaluation_id is required")
        response = self._rpc(
            "GetEvaluation",
            {"evaluationId": evaluation_id},
            not_found_subject=f"Evaluation not found: {evaluation_id}",
        )
        return _evaluation_from_response(response, self)

    @_instrumented("chalkcompute.evaluation.list")
    def list(self, *, limit: int | None = None) -> Iterator[Evaluation]:
        """Iterate evaluations, newest first, paging transparently."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        remaining = limit
        cursor: str | None = None
        while remaining is None or remaining > 0:
            request: dict[str, Any] = {
                "limit": min(remaining, 100) if remaining is not None else 100
            }
            if cursor is not None:
                request["cursor"] = cursor
            response = self._rpc("ListEvaluations", request)
            raw_evaluations = response.get("evaluations", [])
            if not isinstance(raw_evaluations, list):
                raise EvaluationError("ListEvaluations returned invalid evaluations")
            for raw in raw_evaluations:
                evaluation = _evaluation_from_dict(raw, self)
                yield evaluation
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            cursor = _optional_string(response.get("nextCursor"))
            if cursor is None:
                return

    @_instrumented("chalkcompute.evaluation.run")
    def run(
        self,
        evaluation_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationRun:
        """Queue a run for an existing evaluation."""
        evaluation_id = evaluation_id.strip()
        if not evaluation_id:
            raise ValueError("evaluation_id is required")
        request: dict[str, Any] = {"evaluationId": evaluation_id}
        if metadata is not None:
            request["metadata"] = _metadata_dict(metadata)
        response = self._rpc("CreateEvaluationRun", request)
        return _evaluation_run_from_response(response, self)

    @_instrumented("chalkcompute.evaluation_run.get")
    def get_run(self, evaluation_run_id: str) -> EvaluationRun:
        """Fetch an evaluation run by identifier."""
        evaluation_run_id = evaluation_run_id.strip()
        if not evaluation_run_id:
            raise ValueError("evaluation_run_id is required")
        response = self._rpc(
            "GetEvaluationRun",
            {"evaluationRunId": evaluation_run_id},
            not_found_subject=f"Evaluation run not found: {evaluation_run_id}",
        )
        return _evaluation_run_from_response(response, self)

    @_instrumented("chalkcompute.evaluation_run.list")
    def list_runs(
        self,
        *,
        evaluation_id: str | None = None,
        limit: int | None = None,
    ) -> Iterator[EvaluationRun]:
        """Iterate evaluation runs, optionally filtered by evaluation."""
        if evaluation_id is not None and not evaluation_id.strip():
            raise ValueError("evaluation_id must not be empty")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        remaining = limit
        cursor: str | None = None
        while remaining is None or remaining > 0:
            request: dict[str, Any] = {
                "limit": min(remaining, 100) if remaining is not None else 100
            }
            if evaluation_id is not None:
                request["evaluationId"] = evaluation_id.strip()
            if cursor is not None:
                request["cursor"] = cursor
            response = self._rpc("ListEvaluationRuns", request)
            raw_runs = response.get("evaluationRuns", [])
            if not isinstance(raw_runs, list):
                raise EvaluationError(
                    "ListEvaluationRuns returned invalid evaluation runs"
                )
            for raw in raw_runs:
                run = _evaluation_run_from_dict(raw, self)
                yield run
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            cursor = _optional_string(response.get("nextCursor"))
            if cursor is None:
                return

    def _rpc(
        self,
        method: str,
        request: dict[str, Any],
        *,
        not_found_subject: str | None = None,
    ) -> dict[str, Any]:
        response = self._connect.rpc(_EVALUATION_SERVICE, method, request)
        if response.status_code != 200:
            message = _response_error_message(response)
            if response.status_code == 404 and not_found_subject is not None:
                if message:
                    not_found_subject = f"{not_found_subject}: {message}"
                raise EvaluationNotFoundError(
                    not_found_subject, status_code=response.status_code
                )
            detail = f": {message}" if message else ""
            raise EvaluationError(
                f"{method} failed with HTTP {response.status_code}{detail}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise EvaluationError(f"{method} returned an invalid response")
        return payload


def _resolve_dataset_revision(
    dataset: DatasetRevisionRef | DatasetRevisionLike | DatasetNameLike,
) -> DatasetRevisionRef:
    if isinstance(dataset, DatasetRevisionRef):
        return dataset
    dataset_id = getattr(dataset, "dataset_id", None)
    revision_id = getattr(dataset, "revision_id", None)
    if dataset_id is not None and revision_id is not None:
        return DatasetRevisionRef(
            dataset_id=str(dataset_id),
            revision_id=str(revision_id),
        )
    dataset_name = getattr(dataset, "dataset_name", None)
    if dataset_name is not None:
        return DatasetRevisionRef(dataset_name=str(dataset_name))
    raise TypeError(
        "dataset must be a DatasetRevisionRef, expose dataset_id and revision_id "
        "attributes, or expose a dataset_name attribute"
    )


def _resolve_function_version(function: DeployedFunction, *, role: str) -> str:
    if not isinstance(function, RemoteFunction):
        raise TypeError(
            f"{role} must be a RemoteFunction returned by @chalkcompute.function "
            "or RemoteFunction.from_name()/from_id()"
        )
    version_info = function.version_info
    if version_info is None:
        raise EvaluationError(
            f"{role} function {function!r} is not deployed; call .deploy() before "
            "creating the evaluation"
        )
    return version_info.id


def _metadata_dict(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    value = dict(metadata)
    if any(not isinstance(key, str) for key in value):
        raise TypeError("metadata keys must be strings")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("metadata must be JSON-serializable") from exc
    return value


def _evaluation_from_response(
    response: Mapping[str, Any], client: EvaluationClient
) -> Evaluation:
    raw = response.get("evaluation")
    if not isinstance(raw, Mapping):
        raise EvaluationError("evaluation response did not include an evaluation")
    return _evaluation_from_dict(raw, client)


def _evaluation_from_dict(
    raw: Mapping[str, Any], client: EvaluationClient
) -> Evaluation:
    scorer_values = raw.get("scorers", [])
    if not isinstance(scorer_values, list):
        raise EvaluationError("evaluation response contained invalid scorers")
    scorers = tuple(_scorer_from_dict(value) for value in scorer_values)
    return Evaluation(
        id=_required_string(raw, "id"),
        name=_required_string(raw, "name"),
        dataset=DatasetRevisionRef(
            dataset_id=_required_string(raw, "datasetId"),
            revision_id=_required_string(raw, "datasetRevisionId"),
        ),
        task_function_version_id=_required_string(raw, "taskFunctionVersionId"),
        scorers=scorers,
        metadata=_mapping_dict(raw.get("metadata")),
        team_id=_optional_string(raw.get("teamId")),
        environment_id=_optional_string(raw.get("environmentId")),
        created_by=_optional_string(raw.get("createdBy")),
        created_at=_parse_timestamp(raw.get("createdAt")),
        _client=client,
    )


def _scorer_from_dict(raw: object) -> EvaluationScorer:
    if not isinstance(raw, Mapping):
        raise EvaluationError("evaluation response contained an invalid scorer")
    spec = raw.get("spec")
    if not isinstance(spec, Mapping):
        spec = {}
    function = spec.get("function")
    builtin = spec.get("builtin")
    return EvaluationScorer(
        id=_required_string(raw, "id"),
        function_version_id=(
            _optional_string(function.get("functionVersionId"))
            if isinstance(function, Mapping)
            else None
        ),
        builtin_id=(
            _optional_string(builtin.get("id"))
            if isinstance(builtin, Mapping)
            else None
        ),
    )


def _evaluation_run_from_response(
    response: Mapping[str, Any], client: EvaluationClient
) -> EvaluationRun:
    raw = response.get("evaluationRun")
    if not isinstance(raw, Mapping):
        raise EvaluationError(
            "evaluation run response did not include an evaluation run"
        )
    return _evaluation_run_from_dict(raw, client)


def _evaluation_run_from_dict(
    raw: Mapping[str, Any], client: EvaluationClient
) -> EvaluationRun:
    return EvaluationRun(
        id=_required_string(raw, "id"),
        evaluation_id=_required_string(raw, "evaluationId"),
        status=_parse_run_status(raw.get("status")),
        metadata=_mapping_dict(raw.get("metadata")),
        team_id=_optional_string(raw.get("teamId")),
        environment_id=_optional_string(raw.get("environmentId")),
        created_by=_optional_string(raw.get("createdBy")),
        created_at=_parse_timestamp(raw.get("createdAt")),
        started_at=_parse_timestamp(raw.get("startedAt")),
        completed_at=_parse_timestamp(raw.get("completedAt")),
        result_dataset_id=_optional_string(raw.get("resultDatasetId")),
        result_dataset_revision_id=_optional_string(raw.get("resultDatasetRevisionId")),
        error_message=_optional_string(raw.get("errorMessage")),
        _client=client,
    )


def _parse_run_status(value: object) -> EvaluationRunStatus:
    if isinstance(value, int):
        return {
            1: EvaluationRunStatus.PENDING,
            2: EvaluationRunStatus.RUNNING,
            3: EvaluationRunStatus.SUCCEEDED,
            4: EvaluationRunStatus.FAILED,
            5: EvaluationRunStatus.CANCELED,
        }.get(value, EvaluationRunStatus.UNSPECIFIED)
    if isinstance(value, str):
        normalized = value.removeprefix("EVALUATION_RUN_STATUS_").lower()
        try:
            return EvaluationRunStatus(normalized)
        except ValueError:
            pass
    return EvaluationRunStatus.UNSPECIFIED


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(
            f"invalid timestamp in evaluation response: {value!r}"
        ) from exc


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = _optional_string(value.get(key))
    if result is None:
        raise EvaluationError(f"evaluation response is missing {key}")
    return result


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _mapping_dict(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EvaluationError("evaluation response contained invalid metadata")
    return dict(value)


def _response_error_message(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return str(getattr(response, "text", "")).strip()
    if isinstance(payload, Mapping):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return str(getattr(response, "text", "")).strip()
