from __future__ import annotations

import uuid
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._evaluation import (
    DatasetRevisionRef,
    EvaluationClient,
    EvaluationError,
    EvaluationNotFoundError,
    EvaluationRunExecutionMode,
    EvaluationRunFailedError,
    EvaluationRunStatus,
    EvaluationScorerResult,
    EvaluationSuite,
    SqlExpression,
)
from chalkcompute import scorers
from chalkcompute._arrow_serde import arrow_encoder_for_annotation
from chalkcompute._function import FunctionVersionInfo, RemoteFunction, _build_schemas
from chalkcompute._gen.chalk.evaluation.v1 import service_pb as evaluation_pb
from tests._fakes import FakeMethod, StubbedConnectClient


def _message(message_type: type[Any], payload: Mapping[str, Any]) -> Any:
    return message_type.from_json(json.dumps(payload))


class _FakeConnectClient(StubbedConnectClient):
    def __init__(self, responses: Mapping[str, list[Any]]) -> None:
        self._responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: FakeMethod, request: Any, **_kwargs: Any) -> Any:
        self.calls.append((method.name, request))
        try:
            outcome = self._responses[method.name].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected RPC call: {method.name}") from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _remote_function(
    version_id: str, *, name: str, has_local_function: bool = True
) -> RemoteFunction:
    function = RemoteFunction._make_empty()
    function._has_local_func = has_local_function
    function._func_name = name
    function._version_info = FunctionVersionInfo(
        id=version_id,
        function_name=name,
        version=2,
        scaling_group_name=f"sg-{name}",
    )
    return function


def _evaluation_dict() -> dict[str, Any]:
    return {
        "id": "eval-1",
        "teamId": "team-1",
        "environmentId": "env-1",
        "name": "Support quality",
        "suiteId": "suite-1",
        "datasetId": "dataset-1",
        "datasetRevisionId": "revision-1",
        "taskFunctionVersionId": "fn-task-v2",
        "scorers": [
            {
                "id": "scorer-1",
                "spec": {
                    "function": {"functionVersionId": "fn-score-v2"},
                },
            }
        ],
        "metadata": {"suite": "release"},
        "createdBy": "user-1",
        "createdAt": "2026-08-14T18:22:00Z",
    }


def _suite_dict() -> dict[str, Any]:
    return {
        "id": "suite-1",
        "teamId": "team-1",
        "environmentId": "env-1",
        "name": "Release",
        "createdBy": "user-1",
        "createdAt": "2026-08-18T21:00:00Z",
    }


def _run_dict(
    status: str,
    *,
    error_message: str | None = None,
    result: bool = False,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "run-1",
        "teamId": "team-1",
        "environmentId": "env-1",
        "evaluationId": "eval-1",
        "status": status,
        "metadata": {"git_sha": "abc123"},
        "createdAt": "2026-08-14T18:23:00Z",
    }
    if error_message is not None:
        value["errorMessage"] = error_message
    if result:
        value.update(
            {
                "completedAt": "2026-08-14T18:24:00Z",
                "resultDatasetId": "result-dataset",
                "resultDatasetRevisionId": "result-revision",
            }
        )
    return value


def test_scorer_result_declares_arrow_contract_and_roundtrips() -> None:
    result = EvaluationScorerResult(
        score=1,
        metadata={"total_span_count": 12, "llm_span_count": 3},
    )

    def scorer() -> EvaluationScorerResult:
        return result

    _, output_schema = _build_schemas(scorer)
    result_field = output_schema.columns[0]
    assert result_field.arrow_type.has_field("struct")
    fields = result_field.arrow_type.arrow_type_enum.value.sub_field_types
    assert [field.name for field in fields] == ["score", "metadata"]
    assert fields[0].arrow_type.has_field("float64")
    assert fields[0].nullable is False
    assert fields[1].arrow_type.has_field("large_utf8")
    assert fields[1].nullable is True

    encoded = result.__chalk_serialize__()
    assert isinstance(encoded, pa.StructScalar)
    assert encoded.as_py() == {
        "score": 1.0,
        "metadata": '{"llm_span_count":3,"total_span_count":12}',
    }
    assert EvaluationScorerResult.__chalk_deserialize__(encoded) == result


def test_scorer_result_list_declares_arrow_contract_and_encodes_empty_list() -> None:
    def scorer() -> list[EvaluationScorerResult]:
        return []

    _, output_schema = _build_schemas(scorer)
    result_type = output_schema.columns[0].arrow_type
    assert result_type.has_field("large_list")
    item_field = result_type.arrow_type_enum.value.field_type
    assert item_field.nullable is False
    assert [
        field.name
        for field in item_field.arrow_type.arrow_type_enum.value.sub_field_types
    ] == [
        "score",
        "metadata",
    ]

    encoder = arrow_encoder_for_annotation(list[EvaluationScorerResult])
    assert encoder is not None
    encoded = encoder([])
    assert isinstance(encoded, pa.LargeListScalar)
    assert encoded.as_py() == []
    assert encoded.type.value_type.equals(
        EvaluationScorerResult.__chalk_pyarrow_type__()
    )


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf")])
def test_scorer_result_rejects_invalid_scores(score: float) -> None:
    with pytest.raises(ValueError):
        EvaluationScorerResult(score=score)


def test_scorer_result_rejects_non_json_metadata() -> None:
    with pytest.raises(TypeError, match="JSON-serializable"):
        EvaluationScorerResult(score=0.5, metadata={"unsupported": object()})


def test_create_pins_direct_function_versions() -> None:
    connect = _FakeConnectClient(
        {
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    task = _remote_function("fn-task-v2", name="support-answer")
    scorer = _remote_function("fn-score-v2", name="brand-alignment")

    evaluation = client.create(
        "  Support quality  ",
        dataset=DatasetRevisionRef("dataset-1", "revision-1"),
        task=task,
        scorers=[scorer],
        metadata={"suite": "release"},
        suite_id="  suite-1  ",
    )

    assert evaluation.id == "eval-1"
    assert evaluation.dataset_id == "dataset-1"
    assert evaluation.dataset_revision_id == "revision-1"
    assert evaluation.task_function_version_id == "fn-task-v2"
    assert evaluation.suite_id == "suite-1"
    assert evaluation.scorers[0].function_version_id == "fn-score-v2"
    assert evaluation.created_at is not None
    assert evaluation.created_at.isoformat() == "2026-08-14T18:22:00+00:00"
    assert len(connect.calls) == 1
    method, request = connect.calls[0]
    assert method == "create_evaluation"
    assert isinstance(request, evaluation_pb.CreateEvaluationRequest)
    assert request.name == "Support quality"
    assert request.dataset_id == "dataset-1"
    assert request.dataset_revision_id == "revision-1"
    assert request.task_function_version_id == "fn-task-v2"
    assert request.suite_id == "suite-1"
    assert request.scorers[0].implementation is not None
    assert request.scorers[0].implementation.value.function_version_id == "fn-score-v2"
    assert json.loads(request.metadata.to_json()) == {"suite": "release"}


def test_create_sends_sql_scorers_alongside_function_scorers() -> None:
    evaluation_dict = _evaluation_dict()
    evaluation_dict["scorers"].append(
        {
            "id": "scorer-2",
            "spec": {
                "sqlExpression": {
                    "name": "Exact match",
                    "expression": '"output" = "expected"',
                }
            },
        }
    )
    connect = _FakeConnectClient(
        {
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": evaluation_dict},
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluation = client.create(
        "Support quality",
        dataset=DatasetRevisionRef("dataset-1", "revision-1"),
        task=_remote_function("fn-task-v2", name="support-answer"),
        scorers=[
            _remote_function("fn-score-v2", name="brand-alignment"),
            SqlExpression("  Exact match  ", '  "output" = "expected"  '),
        ],
    )

    _, request = connect.calls[0]
    assert request.scorers[0].implementation.field == "function"
    assert request.scorers[1].implementation.field == "sql_expression"
    assert request.scorers[1].implementation.value.name == "Exact match"
    assert request.scorers[1].implementation.value.expression == '"output" = "expected"'
    # The scorer comes back as its expression, with no function version.
    assert evaluation.scorers[1].sql_name == "Exact match"
    assert evaluation.scorers[1].sql_expression == '"output" = "expected"'
    assert evaluation.scorers[1].function_version_id is None
    assert evaluation.scorers[0].sql_expression is None


@pytest.mark.parametrize(
    ("name", "expression"),
    [
        ("", "1.0"),
        ("   ", "1.0"),
        ("???", "1.0"),
        ("grade", ""),
        ("grade", "1.0; DROP TABLE t"),
        ("grade", "1" * 4001),
    ],
)
def test_sql_scorer_rejects_what_the_server_would(name: str, expression: str) -> None:
    with pytest.raises(ValueError):
        SqlExpression(name, expression)


def test_scorers_sql_names_a_hand_written_expression() -> None:
    assert scorers.sql("Exact", '"output" = "expected"') == SqlExpression(
        "Exact", '"output" = "expected"'
    )


def test_create_and_list_suites() -> None:
    connect = _FakeConnectClient(
        {
            "create_evaluation_suite": [
                _message(
                    evaluation_pb.CreateEvaluationSuiteResponse,
                    {"suite": _suite_dict()},
                )
            ],
            "list_evaluation_suites": [
                _message(
                    evaluation_pb.ListEvaluationSuitesResponse,
                    {"suites": [_suite_dict()]},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    created = client.create_suite("  Release  ")
    suites = client.list_suites()

    assert isinstance(created, EvaluationSuite)
    assert created.id == "suite-1"
    assert created.name == "Release"
    assert created.created_at is not None
    assert created.created_at.isoformat() == "2026-08-18T21:00:00+00:00"
    assert suites == [created]
    create_request = connect.calls[0][1]
    list_request = connect.calls[1][1]
    assert isinstance(create_request, evaluation_pb.CreateEvaluationSuiteRequest)
    assert create_request.name == "Release"
    assert isinstance(list_request, evaluation_pb.ListEvaluationSuitesRequest)


def test_suite_handle_filters_evaluations_and_runs() -> None:
    connect = _FakeConnectClient(
        {
            "list_evaluation_suites": [
                _message(
                    evaluation_pb.ListEvaluationSuitesResponse,
                    {"suites": [_suite_dict()]},
                )
            ],
            "list_evaluations": [
                _message(
                    evaluation_pb.ListEvaluationsResponse,
                    {"evaluations": [_evaluation_dict()]},
                )
            ],
            "list_evaluation_runs": [
                _message(
                    evaluation_pb.ListEvaluationRunsResponse,
                    {"evaluationRuns": [_run_dict("EVALUATION_RUN_STATUS_SUCCEEDED")]},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    suite = client.list_suites()[0]

    evaluations = list(suite.evaluations(limit=1))
    runs = list(suite.runs(limit=1))

    assert [evaluation.id for evaluation in evaluations] == ["eval-1"]
    assert [run.id for run in runs] == ["run-1"]
    evaluations_request = connect.calls[1][1]
    runs_request = connect.calls[2][1]
    assert isinstance(evaluations_request, evaluation_pb.ListEvaluationsRequest)
    assert evaluations_request.suite_id == "suite-1"
    assert isinstance(runs_request, evaluation_pb.ListEvaluationRunsRequest)
    assert runs_request.suite_id == "suite-1"


def test_create_accepts_chalkpy_shaped_revision_and_reference_handles() -> None:
    @dataclass
    class Revision:
        dataset_id: uuid.UUID
        revision_id: uuid.UUID

    dataset_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    response = _evaluation_dict()
    response["datasetId"] = str(dataset_id)
    response["datasetRevisionId"] = str(revision_id)
    connect = _FakeConnectClient(
        {
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": response},
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluation = client.create(
        "Referenced functions",
        dataset=Revision(dataset_id, revision_id),
        task=_remote_function(
            "fn-task-v2", name="support-answer", has_local_function=False
        ),
        scorers=[
            _remote_function(
                "fn-score-v2", name="brand-alignment", has_local_function=False
            )
        ],
    )

    assert evaluation.dataset == DatasetRevisionRef(str(dataset_id), str(revision_id))
    request = connect.calls[0][1]
    assert isinstance(request, evaluation_pb.CreateEvaluationRequest)
    assert request.dataset_id == str(dataset_id)
    assert request.dataset_revision_id == str(revision_id)


def test_create_resolves_dataset_name_to_latest_revision() -> None:
    connect = _FakeConnectClient(
        {
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluation = client.create(
        "Latest goldens",
        dataset=DatasetRevisionRef(dataset_name="  support-goldens  "),
        task=_remote_function("fn-task-v2", name="support-answer"),
        scorers=[_remote_function("fn-score-v2", name="brand-alignment")],
    )

    assert evaluation.dataset == DatasetRevisionRef("dataset-1", "revision-1")
    request = connect.calls[0][1]
    assert isinstance(request, evaluation_pb.CreateEvaluationRequest)
    assert request.dataset_name == "support-goldens"
    assert not request.has_field("dataset_id")
    assert not request.has_field("dataset_revision_id")
    assert not request.has_field("suite_id")


def test_dataset_revision_ref_rejects_name_with_revision_ids() -> None:
    with pytest.raises(ValueError, match="either dataset_name"):
        DatasetRevisionRef(
            dataset_id="dataset-1",
            revision_id="revision-1",
            dataset_name="support-goldens",
        )


def test_create_rejects_undeployed_function_without_rpc() -> None:
    connect = _FakeConnectClient({})
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(EvaluationError, match=r"not deployed; call \.deploy\(\)"):
        client.create(
            "Undeployed",
            dataset=DatasetRevisionRef("dataset-1", "revision-1"),
            task=RemoteFunction._make_empty(),
            scorers=[_remote_function("fn-score-v2", name="score")],
        )

    assert connect.calls == []


def test_create_rejects_bare_callable() -> None:
    connect = _FakeConnectClient({})
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="task must be a RemoteFunction"):
        client.create(
            "Bare callable",
            dataset=DatasetRevisionRef("dataset-1", "revision-1"),
            task=lambda value: value,  # type: ignore[arg-type]
            scorers=[_remote_function("fn-score-v2", name="score")],
        )


def test_run_waits_until_success_and_exposes_result_dataset() -> None:
    connect = _FakeConnectClient(
        {
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ],
            "create_evaluation_run": [
                _message(
                    evaluation_pb.CreateEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_PENDING")},
                )
            ],
            "get_evaluation_run": [
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_RUNNING")},
                ),
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {
                        "evaluationRun": _run_dict(
                            "EVALUATION_RUN_STATUS_SUCCEEDED", result=True
                        )
                    },
                ),
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    evaluation = client.create(
        "Support quality",
        dataset=DatasetRevisionRef("dataset-1", "revision-1"),
        task=_remote_function("fn-task-v2", name="task"),
        scorers=[_remote_function("fn-score-v2", name="score")],
    )

    run = evaluation.run(metadata={"git_sha": "abc123"}).wait(poll_interval=0)

    assert run.status is EvaluationRunStatus.SUCCEEDED
    assert run.is_terminal
    assert run.result_dataset == DatasetRevisionRef("result-dataset", "result-revision")
    assert [method for method, _request in connect.calls] == [
        "create_evaluation",
        "create_evaluation_run",
        "get_evaluation_run",
        "get_evaluation_run",
    ]
    run_request = connect.calls[1][1]
    assert isinstance(run_request, evaluation_pb.CreateEvaluationRunRequest)
    assert run_request.evaluation_id == "eval-1"
    assert json.loads(run_request.metadata.to_json()) == {"git_sha": "abc123"}


def _create_run_client(run: dict[str, Any]) -> tuple[_FakeConnectClient, Any]:
    connect = _FakeConnectClient(
        {
            "get_evaluation": [
                _message(
                    evaluation_pb.GetEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ],
            "create_evaluation_run": [
                _message(
                    evaluation_pb.CreateEvaluationRunResponse,
                    {"evaluationRun": run},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    return connect, client.get("eval-1")


def test_run_omits_execution_mode_by_default() -> None:
    connect, evaluation = _create_run_client(_run_dict("EVALUATION_RUN_STATUS_PENDING"))

    run = evaluation.run()

    run_request = connect.calls[-1][1]
    assert isinstance(run_request, evaluation_pb.CreateEvaluationRunRequest)
    assert "executionMode" not in json.loads(run_request.to_json())
    assert run.execution_mode is EvaluationRunExecutionMode.UNSPECIFIED


@pytest.mark.parametrize(
    ("execution_mode", "expected_pb", "expected_json"),
    [
        (
            EvaluationRunExecutionMode.WORKFLOW,
            evaluation_pb.EvaluationRunExecutionMode.WORKFLOW,
            "EVALUATION_RUN_EXECUTION_MODE_WORKFLOW",
        ),
        (
            "workflow",
            evaluation_pb.EvaluationRunExecutionMode.WORKFLOW,
            "EVALUATION_RUN_EXECUTION_MODE_WORKFLOW",
        ),
        (
            "on_read",
            evaluation_pb.EvaluationRunExecutionMode.ON_READ,
            "EVALUATION_RUN_EXECUTION_MODE_ON_READ",
        ),
    ],
)
def test_run_sends_execution_mode_and_exposes_it_on_run(
    execution_mode: EvaluationRunExecutionMode | str,
    expected_pb: evaluation_pb.EvaluationRunExecutionMode,
    expected_json: str,
) -> None:
    connect, evaluation = _create_run_client(
        {
            **_run_dict("EVALUATION_RUN_STATUS_PENDING"),
            "executionMode": expected_json,
        }
    )

    run = evaluation.run(execution_mode=execution_mode)  # type: ignore[arg-type]

    run_request = connect.calls[-1][1]
    assert isinstance(run_request, evaluation_pb.CreateEvaluationRunRequest)
    assert run_request.execution_mode == expected_pb
    assert json.loads(run_request.to_json())["executionMode"] == expected_json
    assert run.execution_mode is EvaluationRunExecutionMode(execution_mode)


@pytest.mark.parametrize("execution_mode", ["unspecified", "eventually"])
def test_run_rejects_invalid_execution_mode(execution_mode: str) -> None:
    connect, evaluation = _create_run_client(_run_dict("EVALUATION_RUN_STATUS_PENDING"))

    with pytest.raises(ValueError, match="execution_mode"):
        evaluation.run(execution_mode=execution_mode)  # type: ignore[arg-type]

    assert [method for method, _request in connect.calls] == ["get_evaluation"]


def test_wait_raises_typed_error_for_failed_run() -> None:
    connect = _FakeConnectClient(
        {
            "get_evaluation_run": [
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {
                        "evaluationRun": _run_dict(
                            "EVALUATION_RUN_STATUS_FAILED",
                            error_message="scorer timed out",
                        )
                    },
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    run = client.get_run("run-1")

    with pytest.raises(EvaluationRunFailedError, match="scorer timed out") as raised:
        run.wait()

    assert raised.value.run is run


def test_get_maps_not_found_to_typed_error() -> None:
    connect = _FakeConnectClient(
        {"get_evaluation": [ConnectRpcError(Code.NOT_FOUND, "evaluation not found")]}
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(EvaluationNotFoundError, match="evaluation not found"):
        client.get("missing")


def test_list_evaluations_pages_and_honors_limit() -> None:
    second = _evaluation_dict()
    second["id"] = "eval-2"
    connect = _FakeConnectClient(
        {
            "list_evaluations": [
                _message(
                    evaluation_pb.ListEvaluationsResponse,
                    {
                        "evaluations": [_evaluation_dict()],
                        "nextCursor": "next-page",
                    },
                ),
                _message(
                    evaluation_pb.ListEvaluationsResponse,
                    {"evaluations": [second]},
                ),
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluations = list(client.list(limit=2))

    assert [evaluation.id for evaluation in evaluations] == ["eval-1", "eval-2"]
    first_request = connect.calls[0][1]
    second_request = connect.calls[1][1]
    assert isinstance(first_request, evaluation_pb.ListEvaluationsRequest)
    assert isinstance(second_request, evaluation_pb.ListEvaluationsRequest)
    assert first_request.limit == 2 and not first_request.has_field("cursor")
    assert second_request.limit == 1 and second_request.cursor == "next-page"


def test_metadata_must_be_json_serializable() -> None:
    connect = _FakeConnectClient({})
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="JSON-serializable"):
        client.create(
            "Invalid metadata",
            dataset=DatasetRevisionRef("dataset-1", "revision-1"),
            task=_remote_function("fn-task-v2", name="task"),
            scorers=[_remote_function("fn-score-v2", name="score")],
            metadata={"bad": object()},
        )


def test_rescore_with_new_scorers_creates_a_sibling_evaluation() -> None:
    rescored_evaluation = {
        **_evaluation_dict(),
        "id": "eval-2",
        "name": "Support quality (rescored)",
        "scorers": [
            {"id": "scorer-2", "spec": {"function": {"functionVersionId": "fn-new-v1"}}}
        ],
    }
    connect = _FakeConnectClient(
        {
            "get_evaluation_run": [
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {
                        "evaluationRun": _run_dict(
                            "EVALUATION_RUN_STATUS_SUCCEEDED", result=True
                        )
                    },
                )
            ],
            "get_evaluation": [
                _message(
                    evaluation_pb.GetEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ],
            "create_evaluation": [
                _message(
                    evaluation_pb.CreateEvaluationResponse,
                    {"evaluation": rescored_evaluation},
                )
            ],
            "rescore_evaluation_run": [
                _message(
                    evaluation_pb.RescoreEvaluationRunResponse,
                    {
                        "evaluationRun": {
                            **_run_dict("EVALUATION_RUN_STATUS_PENDING"),
                            "id": "run-2",
                            "evaluationId": "eval-2",
                        }
                    },
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    run = client.get_run("run-1")

    rescored = run.rescore(
        scorers=[_remote_function("fn-new-v1", name="new_score")],
        metadata={"why": "new scorer"},
    )

    assert rescored.id == "run-2"
    assert rescored.evaluation_id == "eval-2"
    assert [name for name, _ in connect.calls] == [
        "get_evaluation_run",
        "get_evaluation",
        "create_evaluation",
        "rescore_evaluation_run",
    ]
    create_request = connect.calls[2][1]
    # Same dataset revision, task and suite; only the scorers change.
    assert create_request.name == "Support quality (rescored)"
    assert create_request.dataset_id == "dataset-1"
    assert create_request.dataset_revision_id == "revision-1"
    assert create_request.task_function_version_id == "fn-task-v2"
    assert create_request.suite_id == "suite-1"
    assert [
        spec.implementation.value.function_version_id
        for spec in create_request.scorers
        if spec.implementation is not None
    ] == ["fn-new-v1"]
    rescore_request = connect.calls[3][1]
    assert rescore_request.evaluation_run_id == "run-1"
    assert rescore_request.evaluation_id == "eval-2"
    assert json.loads(rescore_request.metadata.to_json()) == {"why": "new scorer"}


def test_rescore_defaults_to_the_runs_own_evaluation() -> None:
    connect = _FakeConnectClient(
        {
            "rescore_evaluation_run": [
                _message(
                    evaluation_pb.RescoreEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_PENDING")},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    client.rescore("run-1")

    request = connect.calls[0][1]
    assert request.evaluation_run_id == "run-1"
    assert "evaluationId" not in json.loads(request.to_json())


def test_evaluation_rescore_scores_another_run_into_itself() -> None:
    connect = _FakeConnectClient(
        {
            "get_evaluation": [
                _message(
                    evaluation_pb.GetEvaluationResponse,
                    {"evaluation": _evaluation_dict()},
                )
            ],
            "rescore_evaluation_run": [
                _message(
                    evaluation_pb.RescoreEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_PENDING")},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    evaluation = client.get("eval-1")

    evaluation.rescore("run-0")

    request = connect.calls[1][1]
    assert request.evaluation_run_id == "run-0"
    assert request.evaluation_id == "eval-1"


def test_rescore_rejects_conflicting_arguments_without_rpc() -> None:
    connect = _FakeConnectClient(
        {
            "get_evaluation_run": [
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_SUCCEEDED")},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]
    run = client.get_run("run-1")

    with pytest.raises(ValueError, match="only one of scorers or evaluation"):
        run.rescore(
            scorers=[_remote_function("fn-new-v1", name="new_score")],
            evaluation="eval-2",
        )
    with pytest.raises(ValueError, match="name applies only"):
        run.rescore(name="renamed")
    assert len(connect.calls) == 1


def test_rescore_raises_when_the_server_ignores_the_target_evaluation() -> None:
    connect = _FakeConnectClient(
        {
            "rescore_evaluation_run": [
                _message(
                    evaluation_pb.RescoreEvaluationRunResponse,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_PENDING")},
                )
            ],
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(EvaluationError, match="does not support rescoring"):
        client.rescore("run-0", evaluation_id="eval-2")
