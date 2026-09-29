"""Versioned evaluation definitions and runs.

Evaluations bind a completed Chalk dataset revision to one deployed task
function version and one or more deployed scorer function versions.  Creating
an evaluation persists that immutable definition; :meth:`Evaluation.run`
queues a separate execution of it.

The task and scorers may be handles returned directly by
``@chalkcompute.function`` or references created with
``RemoteFunction.from_name`` / ``RemoteFunction.from_id``.  In either case,
decorated handles wait for their automatic background deployment when their
version is read. Evaluation creation never silently deploys an imperative,
undeployed ``RemoteFunction``.
"""

from __future__ import annotations

import builtins
import json
import math
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from protobuf.wkt import struct_pb

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._connect import ConnectClient
from chalkcompute._function import DeployedFunction, RemoteFunction
from chalkcompute._gen.chalk.evaluation.v1 import service_connect as evaluation_connect
from chalkcompute._gen.chalk.evaluation.v1 import service_pb as evaluation_pb
from chalkcompute._tracing import _instrumented


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

    score: float
    """Normalized score in the inclusive range ``[0, 1]``."""

    metadata: Mapping[str, Any] | None = None
    """Optional JSON-serializable metadata retained with the scored row."""

    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof(
            "struct",
            arrow_pb.Struct(
                sub_field_types=[
                    arrow_pb.Field(
                        name="score",
                        arrow_type=arrow_pb.ArrowType(
                            arrow_type_enum=Oneof("float64", arrow_pb.EmptyMessage())
                        ),
                        nullable=False,
                    ),
                    arrow_pb.Field(
                        name="metadata",
                        arrow_type=arrow_pb.ArrowType(
                            arrow_type_enum=Oneof("large_utf8", arrow_pb.EmptyMessage())
                        ),
                        nullable=True,
                    ),
                ],
            ),
        )
    )
    __chalk_pyarrow_type__ = staticmethod(_scorer_result_pyarrow_type)

    def __post_init__(self) -> None:
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
            {"score": self.score, "metadata": metadata},
            type=_scorer_result_pyarrow_type(),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> EvaluationScorerResult:
        """Decode an Arrow scorer-result struct returned by Chalk Compute."""
        raw = value.as_py()
        metadata = raw.get("metadata")
        return cls(
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


# The server's own bound on a scorer expression; mirrored so an oversized
# expression fails here rather than after a round trip.
_MAX_SQL_EXPRESSION_LENGTH = 4000


@dataclass(frozen=True)
class SqlExpression:
    """A scorer whose score is a ChalkSQL expression, evaluated in the run's query.

    The expression is a select item over the evaluation's own columns: every
    dataset column by name, plus ``output``, the task's result for the row. It
    must yield a number, and unlike a function scorer it deploys nothing and
    carries no metadata.
    """

    name: str
    expression: str

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("SqlExpression: name is required")
        if not any(character.isalnum() for character in name):
            raise ValueError(
                f"SqlExpression: name must contain a letter or digit, not {self.name!r}"
            )
        expression = self.expression.strip()
        if not expression:
            raise ValueError("SqlExpression: expression is required")
        if len(expression) > _MAX_SQL_EXPRESSION_LENGTH:
            raise ValueError(
                f"SqlExpression: expression is longer than "
                f"{_MAX_SQL_EXPRESSION_LENGTH} characters"
            )
        if ";" in expression:
            raise ValueError(
                "SqlExpression: expression is one select item, so it may not contain ';'"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "expression", expression)


@dataclass(frozen=True)
class EvaluationScorer:
    """A versioned scorer attached to an evaluation."""

    id: str
    """Evaluation-local scorer identifier."""

    function_version_id: str | None = None
    """Remote function version backing the scorer, when function-backed."""

    builtin_id: str | None = None
    """Immutable built-in scorer identifier, when built-in."""

    sql_name: str | None = None
    """Scorer name, when the scorer is a ChalkSQL expression."""

    sql_expression: str | None = None
    """The ChalkSQL expression, when the scorer is one."""


class EvaluationRunStatus(str, Enum):
    """Lifecycle state of an evaluation run."""

    UNSPECIFIED = "unspecified"
    PENDING = "pending"
    RUNNING = "running"
    FINALIZING = "finalizing"
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
class EvaluationSuite:
    """A named collection of evaluations and their runs."""

    id: str
    name: str
    team_id: str | None = None
    environment_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    _client: EvaluationClient | None = field(default=None, repr=False, compare=False)

    @classmethod
    def create(cls, name: str, *, chalk_client: Any | None = None) -> EvaluationSuite:
        """Create an evaluation suite in the active environment."""
        return EvaluationClient(chalk_client=chalk_client).create_suite(name)

    @classmethod
    def list_all(cls, *, chalk_client: Any | None = None) -> list[EvaluationSuite]:
        """List the evaluation suites in the active environment."""
        return EvaluationClient(chalk_client=chalk_client).list_suites()

    def evaluations(self, *, limit: int | None = None) -> Iterator[Evaluation]:
        """Iterate the evaluations in this suite, newest first."""
        return self._require_client().list(suite_id=self.id, limit=limit)

    def runs(self, *, limit: int | None = None) -> Iterator[EvaluationRun]:
        """Iterate the evaluation runs in this suite, newest first."""
        return self._require_client().list_runs(suite_id=self.id, limit=limit)

    def _require_client(self) -> EvaluationClient:
        if self._client is None:
            raise EvaluationError(
                "EvaluationSuite has no attached client. Fetch or create it through "
                "EvaluationClient."
            )
        return self._client


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
    suite_id: str | None = None
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
        scorers: Sequence[DeployedFunction | SqlExpression],
        metadata: Mapping[str, Any] | None = None,
        suite_id: str | None = None,
        chalk_client: Any | None = None,
    ) -> Evaluation:
        """Create an evaluation from deployed function handles.

        ``task`` and every function scorer must already be deployed. Handles
        returned by ``@chalkcompute.function`` meet this requirement because
        resolving their version waits for the decorator's background deployment.
        A :class:`SqlExpression` deploys nothing and is sent as its expression.
        """
        return EvaluationClient(chalk_client=chalk_client).create(
            name,
            dataset=dataset,
            task=task,
            scorers=scorers,
            metadata=metadata,
            suite_id=suite_id,
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

    def rescore(
        self,
        run: EvaluationRun | str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationRun:
        """Score a finished run's outputs with this evaluation's scorers.

        The task is not re-run: its outputs are read back out of ``run``'s
        result. ``run`` may belong to this evaluation or to any other that uses
        the same dataset revision and task function version, and the new run is
        recorded under this one.
        """
        run_id = run.id if isinstance(run, EvaluationRun) else run
        return self._require_client().rescore(
            run_id, evaluation_id=self.id, metadata=metadata
        )

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

    def rescore(
        self,
        *,
        scorers: Sequence[DeployedFunction | SqlExpression] | None = None,
        evaluation: Evaluation | str | None = None,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationRun:
        """Score this run's outputs again without re-running the task.

        With no arguments, the outputs are scored by this run's own evaluation's
        scorers. ``scorers`` scores them with new ones instead: an evaluation is
        created from this run's, with the same dataset revision, task and suite
        and only the scorers replaced, named ``name`` or
        ``"<evaluation name> (rescored)"``. ``evaluation`` names an existing
        evaluation to score them with, which must use this run's dataset
        revision and task function version.

        The new run is recorded under whichever evaluation scored it, and is
        read, waited on and summarized like any other of its runs.
        """
        client = self._require_client()
        if scorers is not None and evaluation is not None:
            raise ValueError("pass only one of scorers or evaluation")
        if name is not None and scorers is None:
            raise ValueError("name applies only when scorers are given")
        evaluation_id: str | None
        if scorers is not None:
            source = client.get(self.evaluation_id)
            evaluation_id = client._create(
                name if name is not None else f"{source.name} (rescored)",
                dataset=source.dataset,
                task_function_version_id=source.task_function_version_id,
                scorers=scorers,
                metadata=None,
                suite_id=source.suite_id,
            ).id
        elif isinstance(evaluation, Evaluation):
            evaluation_id = evaluation.id
        else:
            evaluation_id = evaluation
        return client.rescore(self.id, evaluation_id=evaluation_id, metadata=metadata)

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

    @_instrumented("chalkcompute.evaluation_suite.create")
    def create_suite(self, name: str) -> EvaluationSuite:
        """Create an evaluation suite in the active environment."""
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.create_evaluation_suite,
            evaluation_pb.CreateEvaluationSuiteRequest(name=name),
        )
        if response.suite is None:
            raise EvaluationError("CreateEvaluationSuite returned no suite")
        return _evaluation_suite_from_proto(response.suite, self)

    @_instrumented("chalkcompute.evaluation_suite.list")
    def list_suites(self) -> builtins.list[EvaluationSuite]:
        """List the evaluation suites in the active environment."""
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.list_evaluation_suites,
            evaluation_pb.ListEvaluationSuitesRequest(),
        )
        return [_evaluation_suite_from_proto(raw, self) for raw in response.suites]

    @_instrumented("chalkcompute.evaluation.create")
    def create(
        self,
        name: str,
        *,
        dataset: DatasetRevisionRef | DatasetRevisionLike | DatasetNameLike,
        task: DeployedFunction,
        scorers: Sequence[DeployedFunction | SqlExpression],
        metadata: Mapping[str, Any] | None = None,
        suite_id: str | None = None,
    ) -> Evaluation:
        """Persist an evaluation pinned to deployed function versions."""
        return self._create(
            name,
            dataset=dataset,
            task_function_version_id=_resolve_function_version(task, role="task"),
            scorers=scorers,
            metadata=metadata,
            suite_id=suite_id,
        )

    def _create(
        self,
        name: str,
        *,
        dataset: DatasetRevisionRef | DatasetRevisionLike | DatasetNameLike,
        task_function_version_id: str,
        scorers: Sequence[DeployedFunction | SqlExpression],
        metadata: Mapping[str, Any] | None,
        suite_id: str | None,
    ) -> Evaluation:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        dataset_ref = _resolve_dataset_revision(dataset)
        scorer_handles = tuple(scorers)
        if not scorer_handles:
            raise ValueError("at least one scorer is required")
        scorer_specs = [
            _scorer_spec(scorer, role=f"scorer {index}")
            for index, scorer in enumerate(scorer_handles)
        ]
        metadata_dict = _metadata_dict(metadata)
        suite_id = _optional_id(suite_id, name="suite_id")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.create_evaluation,
            evaluation_pb.CreateEvaluationRequest(
                name=name,
                dataset_name=dataset_ref.dataset_name or "",
                dataset_id=dataset_ref.dataset_id or "",
                dataset_revision_id=dataset_ref.revision_id or "",
                task_function_version_id=task_function_version_id,
                scorers=scorer_specs,
                metadata=(
                    None
                    if metadata is None
                    else struct_pb.Struct.from_json(json.dumps(metadata_dict))
                ),
                suite_id=suite_id,
            ),
        )
        if response.evaluation is None:
            raise EvaluationError("CreateEvaluation returned no evaluation")
        return _evaluation_from_proto(response.evaluation, self)

    @_instrumented("chalkcompute.evaluation.get")
    def get(self, evaluation_id: str) -> Evaluation:
        """Fetch an evaluation by identifier."""
        evaluation_id = evaluation_id.strip()
        if not evaluation_id:
            raise ValueError("evaluation_id is required")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.get_evaluation,
            evaluation_pb.GetEvaluationRequest(evaluation_id=evaluation_id),
            not_found_subject=f"Evaluation not found: {evaluation_id}",
        )
        if response.evaluation is None:
            raise EvaluationError("GetEvaluation returned no evaluation")
        return _evaluation_from_proto(response.evaluation, self)

    @_instrumented("chalkcompute.evaluation.list")
    def list(
        self, *, suite_id: str | None = None, limit: int | None = None
    ) -> Iterator[Evaluation]:
        """Iterate evaluations, optionally filtered by suite."""
        suite_id = _optional_id(suite_id, name="suite_id")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        remaining = limit
        cursor: str | None = None
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        while remaining is None or remaining > 0:
            response = self._call(
                stub.list_evaluations,
                evaluation_pb.ListEvaluationsRequest(
                    limit=min(remaining, 100) if remaining is not None else 100,
                    cursor=cursor,
                    suite_id=suite_id,
                ),
            )
            for raw in response.evaluations:
                evaluation = _evaluation_from_proto(raw, self)
                yield evaluation
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            cursor = response.next_cursor or None
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
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.create_evaluation_run,
            evaluation_pb.CreateEvaluationRunRequest(
                evaluation_id=evaluation_id,
                metadata=(
                    None
                    if metadata is None
                    else struct_pb.Struct.from_json(
                        json.dumps(_metadata_dict(metadata))
                    )
                ),
            ),
        )
        if response.evaluation_run is None:
            raise EvaluationError("CreateEvaluationRun returned no run")
        return _evaluation_run_from_proto(response.evaluation_run, self)

    @_instrumented("chalkcompute.evaluation_run.rescore")
    def rescore(
        self,
        evaluation_run_id: str,
        *,
        evaluation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationRun:
        """Queue a run that scores a finished run's outputs without its task.

        ``evaluation_id`` names the evaluation whose scorers score the outputs
        and that the new run is recorded under. It defaults to the finished
        run's own, and otherwise must use the same dataset revision and task
        function version.
        """
        evaluation_run_id = evaluation_run_id.strip()
        if not evaluation_run_id:
            raise ValueError("evaluation_run_id is required")
        evaluation_id = _optional_id(evaluation_id, name="evaluation_id")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.rescore_evaluation_run,
            evaluation_pb.RescoreEvaluationRunRequest(
                evaluation_run_id=evaluation_run_id,
                evaluation_id=evaluation_id,
                metadata=(
                    None
                    if metadata is None
                    else struct_pb.Struct.from_json(
                        json.dumps(_metadata_dict(metadata))
                    )
                ),
            ),
            not_found_subject=(
                f"Evaluation run or evaluation not found: {evaluation_run_id}"
            ),
        )
        if response.evaluation_run is None:
            raise EvaluationError("RescoreEvaluationRun returned no run")
        return _evaluation_run_from_proto(response.evaluation_run, self)

    @_instrumented("chalkcompute.evaluation_run.get")
    def get_run(self, evaluation_run_id: str) -> EvaluationRun:
        """Fetch an evaluation run by identifier."""
        evaluation_run_id = evaluation_run_id.strip()
        if not evaluation_run_id:
            raise ValueError("evaluation_run_id is required")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.get_evaluation_run,
            evaluation_pb.GetEvaluationRunRequest(evaluation_run_id=evaluation_run_id),
            not_found_subject=f"Evaluation run not found: {evaluation_run_id}",
        )
        if response.evaluation_run is None:
            raise EvaluationError("GetEvaluationRun returned no run")
        return _evaluation_run_from_proto(response.evaluation_run, self)

    @_instrumented("chalkcompute.evaluation_run.list")
    def list_runs(
        self,
        *,
        evaluation_id: str | None = None,
        suite_id: str | None = None,
        limit: int | None = None,
    ) -> Iterator[EvaluationRun]:
        """Iterate runs, optionally filtered by evaluation or suite."""
        if evaluation_id is not None and not evaluation_id.strip():
            raise ValueError("evaluation_id must not be empty")
        suite_id = _optional_id(suite_id, name="suite_id")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        remaining = limit
        cursor: str | None = None
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        while remaining is None or remaining > 0:
            response = self._call(
                stub.list_evaluation_runs,
                evaluation_pb.ListEvaluationRunsRequest(
                    evaluation_id=(
                        None if evaluation_id is None else evaluation_id.strip()
                    ),
                    limit=min(remaining, 100) if remaining is not None else 100,
                    cursor=cursor,
                    suite_id=suite_id,
                ),
            )
            for raw in response.evaluation_runs:
                run = _evaluation_run_from_proto(raw, self)
                yield run
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            cursor = response.next_cursor or None
            if cursor is None:
                return

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
                raise EvaluationNotFoundError(
                    not_found_subject, status_code=status_code
                ) from exc
            raise EvaluationError(
                f"{getattr(method, '__name__', 'evaluation RPC')} failed: {exc}",
                status_code=status_code,
            ) from exc


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


def _scorer_spec(
    scorer: DeployedFunction | SqlExpression, *, role: str
) -> evaluation_pb.EvaluationScorerSpec:
    """One scorer as the wire spec: a pinned function version, or an expression."""
    if isinstance(scorer, SqlExpression):
        return evaluation_pb.EvaluationScorerSpec(
            implementation=Oneof(
                "sql_expression",
                evaluation_pb.SqlExpressionScorer(
                    name=scorer.name, expression=scorer.expression
                ),
            )
        )
    return evaluation_pb.EvaluationScorerSpec(
        implementation=Oneof(
            "function",
            evaluation_pb.FunctionScorer(
                function_version_id=_resolve_function_version(scorer, role=role)
            ),
        )
    )


def _resolve_function_version(function: DeployedFunction, *, role: str) -> str:
    if not isinstance(function, RemoteFunction):
        raise TypeError(
            f"{role} must be a RemoteFunction returned by @chalkcompute.function, "
            "RemoteFunction.from_name()/from_id(), or a SqlExpression"
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


def _optional_id(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _evaluation_suite_from_proto(
    raw: evaluation_pb.EvaluationSuite, client: EvaluationClient
) -> EvaluationSuite:
    return EvaluationSuite(
        id=raw.id,
        name=raw.name,
        team_id=raw.team_id or None,
        environment_id=raw.environment_id or None,
        created_by=raw.created_by or None,
        created_at=None if raw.created_at is None else raw.created_at.to_datetime(),
        _client=client,
    )


def _evaluation_from_proto(
    raw: evaluation_pb.Evaluation, client: EvaluationClient
) -> Evaluation:
    scorers = tuple(_scorer_from_proto(value) for value in raw.scorers)
    return Evaluation(
        id=raw.id,
        name=raw.name,
        dataset=DatasetRevisionRef(
            dataset_id=raw.dataset_id,
            revision_id=raw.dataset_revision_id,
        ),
        task_function_version_id=raw.task_function_version_id,
        scorers=scorers,
        metadata=_struct_dict(raw.metadata),
        suite_id=raw.suite_id or None,
        team_id=raw.team_id or None,
        environment_id=raw.environment_id or None,
        created_by=raw.created_by or None,
        created_at=None if raw.created_at is None else raw.created_at.to_datetime(),
        _client=client,
    )


def _scorer_from_proto(raw: evaluation_pb.EvaluationScorer) -> EvaluationScorer:
    implementation = None if raw.spec is None else raw.spec.implementation
    return EvaluationScorer(
        id=raw.id,
        function_version_id=(
            implementation.value.function_version_id or None
            if implementation is not None and implementation.field == "function"
            else None
        ),
        builtin_id=(
            implementation.value.id or None
            if implementation is not None and implementation.field == "builtin"
            else None
        ),
        sql_name=(
            implementation.value.name or None
            if implementation is not None and implementation.field == "sql_expression"
            else None
        ),
        sql_expression=(
            implementation.value.expression or None
            if implementation is not None and implementation.field == "sql_expression"
            else None
        ),
    )


def _evaluation_run_from_proto(
    raw: evaluation_pb.EvaluationRun, client: EvaluationClient
) -> EvaluationRun:
    return EvaluationRun(
        id=raw.id,
        evaluation_id=raw.evaluation_id,
        status=_parse_run_status(raw.status),
        metadata=_struct_dict(raw.metadata),
        team_id=raw.team_id or None,
        environment_id=raw.environment_id or None,
        created_by=raw.created_by or None,
        created_at=None if raw.created_at is None else raw.created_at.to_datetime(),
        started_at=None if raw.started_at is None else raw.started_at.to_datetime(),
        completed_at=(
            None if raw.completed_at is None else raw.completed_at.to_datetime()
        ),
        result_dataset_id=raw.result_dataset_id or None,
        result_dataset_revision_id=raw.result_dataset_revision_id or None,
        error_message=raw.error_message or None,
        _client=client,
    )


def _parse_run_status(value: evaluation_pb.EvaluationRunStatus) -> EvaluationRunStatus:
    try:
        return EvaluationRunStatus(value.name.lower())
    except ValueError as exc:
        raise EvaluationError(f"unknown evaluation run status: {value}") from exc


def _struct_dict(value: struct_pb.Struct | None) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(value.to_json())
    if not isinstance(parsed, dict):
        raise EvaluationError("evaluation response contained invalid metadata")
    return parsed


def _http_status_for_code(code: Code) -> int | None:
    return {
        Code.INVALID_ARGUMENT: 400,
        Code.UNAUTHENTICATED: 401,
        Code.PERMISSION_DENIED: 403,
        Code.NOT_FOUND: 404,
        Code.ALREADY_EXISTS: 409,
        Code.RESOURCE_EXHAUSTED: 429,
        Code.INTERNAL: 500,
        Code.UNAVAILABLE: 503,
    }.get(code)
