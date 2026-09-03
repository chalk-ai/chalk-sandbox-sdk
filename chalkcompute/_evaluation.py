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
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
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


def _finite_number(value: Any, *, owner: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{owner} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{owner} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{owner} must be finite")
    return number


# Key under which a result's criteria travel inside its metadata JSON. The
# column shape stays {score, metadata}, which every deployed server already
# reads, so a rubric needs no server change to be recorded.
_CRITERIA_METADATA_KEY = "criteria"


@dataclass(frozen=True)
class EvaluationScorerResult:
    """The score, optional metadata, and optional rubric criteria a scorer returns.

    Annotating a ``@chalkcompute.function`` with this return type installs its
    Arrow struct contract automatically.
    """

    score: float
    """Normalized score in the inclusive range ``[0, 1]``."""

    metadata: Mapping[str, Any] | None = None
    """Optional JSON-serializable metadata retained with the scored row."""

    criteria: Mapping[str, float | str] | None = None
    """One grade per criterion, numeric or string, recorded under ``metadata["criteria"]``.

    Checked against the function's ``rubric=`` at serialization, when there is one.
    """

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
        score = _finite_number(self.score, owner="score")
        if not 0 <= score <= 1:
            raise ValueError("score must be between 0 and 1")
        object.__setattr__(self, "score", score)

        if self.metadata is not None:
            metadata = _metadata_dict(self.metadata)
            try:
                json.dumps(metadata, allow_nan=False)
            except ValueError as exc:
                raise TypeError("metadata must be JSON-serializable") from exc
            if self.criteria is not None and _CRITERIA_METADATA_KEY in metadata:
                raise ValueError(
                    "metadata['criteria'] is reserved; pass criteria= instead"
                )
            object.__setattr__(self, "metadata", metadata)

        if self.criteria is not None:
            if not isinstance(self.criteria, Mapping):
                raise TypeError("criteria must be a mapping")
            if any(not isinstance(name, str) for name in self.criteria):
                raise TypeError("criteria keys must be strings")
            object.__setattr__(
                self,
                "criteria",
                {
                    name: value
                    if isinstance(value, str)
                    else _finite_number(value, owner=f"criterion {name!r}")
                    for name, value in self.criteria.items()
                },
            )

    def __chalk_serialize__(self) -> Any:
        """Encode this result as the Arrow struct registered in the catalog."""
        import pyarrow as pa

        metadata = None
        if self.metadata is not None or self.criteria is not None:
            combined = dict(self.metadata or {})
            if self.criteria is not None:
                combined[_CRITERIA_METADATA_KEY] = dict(self.criteria)
            metadata = json.dumps(
                combined, allow_nan=False, separators=(",", ":"), sort_keys=True
            )
        return pa.scalar(
            {"score": self.score, "metadata": metadata},
            type=_scorer_result_pyarrow_type(),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> EvaluationScorerResult:
        """Decode an Arrow scorer-result struct returned by Chalk Compute."""
        raw = value.as_py()
        metadata = None if raw.get("metadata") is None else json.loads(raw["metadata"])
        criteria = None
        if isinstance(metadata, dict) and isinstance(
            metadata.get(_CRITERIA_METADATA_KEY), dict
        ):
            criteria = metadata.pop(_CRITERIA_METADATA_KEY)
        return EvaluationScorerResult(
            score=raw["score"], metadata=metadata or None, criteria=criteria
        )


# ---------------------------------------------------------------------------
# Rubrics
# ---------------------------------------------------------------------------

_CRITERION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Schema-metadata key carrying a function's rubric declaration, beside the
# existing ``chalk.function.*`` traits.
RUBRIC_SCHEMA_METADATA_KEY = "chalk.rubric"


@dataclass(frozen=True)
class Criterion:
    """One thing a scorer grades, and the scale it grades on."""

    name: str
    scale: tuple[float, float] = (0.0, 1.0)
    """Inclusive ``(low, high)`` bounds a value must fall within."""
    description: str = ""
    """What the grader is asked. Rendered into judge prompts and shown in Chalk."""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("criterion name must be a string")
        name = self.name.strip()
        if not _CRITERION_NAME_RE.match(name):
            raise ValueError(
                f"invalid criterion name {name!r}: use letters, digits, and "
                "underscores, not starting with a digit"
            )
        if not isinstance(self.description, str):
            raise TypeError("criterion description must be a string")
        if not isinstance(self.scale, tuple) or len(self.scale) != 2:
            raise TypeError("criterion scale must be a (low, high) tuple")
        low = _finite_number(self.scale[0], owner=f"criterion {name!r} scale low")
        high = _finite_number(self.scale[1], owner=f"criterion {name!r} scale high")
        if low >= high:
            raise ValueError(f"criterion {name!r} scale low must be less than high")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "scale", (low, high))


@dataclass(frozen=True)
class Rubric:
    """The criteria a scorer grades on, declared once and versioned with it.

    Pass to ``@chalkcompute.function(rubric=...)``. The declaration travels
    with the function version, and each result's criteria are checked against
    it and recorded under ``metadata["criteria"]``. The score stays the
    scorer's own: a rubric explains it, it never replaces it.

    >>> rubric = cc.Rubric(
    ...     cc.Criterion("directness", scale=(0, 2), description="Shortest path?"),
    ...     cc.Criterion("task_correctness", scale=(0, 2), description="Done?"),
    ... )
    >>> @cc.function(rubric=rubric)
    ... def judge(output: str) -> cc.EvaluationScorerResult:
    ...     return rubric.result(0.75, {"directness": 2, "task_correctness": 1})
    """

    criteria: tuple[Criterion, ...]
    name: str | None = None

    def __init__(self, *criteria: Criterion, name: str | None = None) -> None:
        if not criteria:
            raise ValueError("a rubric requires at least one criterion")
        if any(not isinstance(criterion, Criterion) for criterion in criteria):
            raise TypeError("rubric criteria must be Criterion instances")
        names = [criterion.name for criterion in criteria]
        if len(set(names)) != len(names):
            duplicate = next(n for n in names if names.count(n) > 1)
            raise ValueError(f"duplicate criterion name {duplicate!r}")
        if name is not None and not isinstance(name, str):
            raise TypeError("rubric name must be a string or None")
        object.__setattr__(self, "criteria", tuple(criteria))
        object.__setattr__(self, "name", (name or "").strip() or None)

    @property
    def by_name(self) -> Mapping[str, Criterion]:
        return MappingProxyType({c.name: c for c in self.criteria})

    def result(
        self,
        score: float,
        criteria: Mapping[str, Any],
        *,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationScorerResult:
        """Build a result with the scorer's score and one validated grade per criterion."""
        combined = dict(metadata or {})
        if reason is not None:
            if not isinstance(reason, str):
                raise TypeError("reason must be a string")
            if "reason" in combined:
                raise ValueError("pass reason= or metadata['reason'], not both")
            combined["reason"] = reason
        return EvaluationScorerResult(
            score=score,
            metadata=combined or None,
            criteria=self.validate_criteria(criteria),
        )

    def validate_criteria(
        self, criteria: Mapping[str, Any] | None
    ) -> dict[str, float | str]:
        """Check grades against this rubric; return them in declaration order."""
        if criteria is None:
            raise ValueError(
                "this scorer declares a rubric, so its result must carry "
                "criteria; got None. Build it with Rubric.result(...)."
            )
        if not isinstance(criteria, Mapping):
            raise TypeError("criteria must be a mapping")
        expected, given = set(self.by_name), set(criteria)
        if expected != given:
            missing, unexpected = sorted(expected - given), sorted(given - expected)
            parts = [f"missing {missing}"] if missing else []
            parts += [f"unexpected {unexpected}"] if unexpected else []
            raise ValueError(f"criteria do not match the rubric: {'; '.join(parts)}")
        validated: dict[str, float | str] = {}
        for criterion in self.criteria:
            raw = criteria[criterion.name]
            if isinstance(raw, str):
                validated[criterion.name] = raw
                continue
            value = _finite_number(raw, owner=f"criterion {criterion.name!r}")
            low, high = criterion.scale
            if not low <= value <= high:
                raise ValueError(
                    f"criterion {criterion.name!r} value {value} is outside its scale ({low}, {high})"
                )
            validated[criterion.name] = value
        return validated

    def to_schema_metadata(self) -> str:
        """JSON declaration stored on the function's output schema."""
        return json.dumps(
            {
                "name": self.name,
                "criteria": [
                    {
                        "name": c.name,
                        "scale": list(c.scale),
                        "description": c.description,
                    }
                    for c in self.criteria
                ],
            },
            separators=(",", ":"),
        )

    def bind(self) -> type[EvaluationScorerResult]:
        """A result class whose serializer validates criteria against this rubric.

        Same Arrow contract as ``EvaluationScorerResult``; only the encoder
        differs, so the wire shape is unchanged for every deployed server.
        """
        if (cached := _BOUND_RESULT_CLASSES.get(self)) is not None:
            return cached
        rubric = self

        # Unbound on purpose: the shim calls annotation.__chalk_serialize__(value)
        # with whatever the scorer returned, usually a plain EvaluationScorerResult.
        def serialize(result: EvaluationScorerResult) -> Any:
            rubric.validate_criteria(result.criteria)
            return EvaluationScorerResult.__chalk_serialize__(result)

        bound: type[EvaluationScorerResult] = type(
            "RubricScorerResult",
            (EvaluationScorerResult,),
            {"__chalk_rubric__": rubric, "__chalk_serialize__": serialize},
        )
        _BOUND_RESULT_CLASSES[self] = bound
        return bound


_BOUND_RESULT_CLASSES: dict[Rubric, type[EvaluationScorerResult]] = {}


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
        scorers: Sequence[DeployedFunction],
        metadata: Mapping[str, Any] | None = None,
        suite_id: str | None = None,
        chalk_client: Any | None = None,
    ) -> Evaluation:
        """Create an evaluation from deployed function handles.

        ``task`` and every scorer must already be deployed. Handles returned by
        ``@chalkcompute.function`` meet this requirement because resolving their
        version waits for the decorator's background deployment.
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
        scorers: Sequence[DeployedFunction],
        metadata: Mapping[str, Any] | None = None,
        suite_id: str | None = None,
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
        suite_id = _optional_id(suite_id, name="suite_id")
        stub = self._connect.stub(evaluation_connect.EvaluationServiceClientSync)
        response = self._call(
            stub.create_evaluation,
            evaluation_pb.CreateEvaluationRequest(
                name=name,
                dataset_name=dataset_ref.dataset_name or "",
                dataset_id=dataset_ref.dataset_id or "",
                dataset_revision_id=dataset_ref.revision_id or "",
                task_function_version_id=task_version_id,
                scorers=[
                    evaluation_pb.EvaluationScorerSpec(
                        implementation=Oneof(
                            "function",
                            evaluation_pb.FunctionScorer(
                                function_version_id=version_id
                            ),
                        )
                    )
                    for version_id in scorer_version_ids
                ],
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
