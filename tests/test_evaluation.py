from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest

from chalkcompute._evaluation import (
    DatasetRevisionRef,
    EvaluationClient,
    EvaluationError,
    EvaluationNotFoundError,
    EvaluationRunFailedError,
    EvaluationRunStatus,
    EvaluationScorerResult,
)
from chalkcompute._arrow_serde import arrow_encoder_for_annotation
from chalkcompute._function import FunctionVersionInfo, RemoteFunction, _build_schemas
from tests._typed_connect import TypedConnectFakeMixin


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Mapping[str, Any] | None = None,
        *,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = dict(payload or {})
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeConnectClient(TypedConnectFakeMixin):
    def __init__(self, responses: Mapping[str, list[_FakeResponse]]) -> None:
        self._responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def rpc(self, service: str, method: str, request: dict[str, Any]) -> _FakeResponse:
        self.calls.append((service, method, request))
        try:
            return self._responses[method].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected RPC call: {method}") from exc


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
        {"CreateEvaluation": [_FakeResponse(200, {"evaluation": _evaluation_dict()})]}
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
    )

    assert evaluation.id == "eval-1"
    assert evaluation.dataset_id == "dataset-1"
    assert evaluation.dataset_revision_id == "revision-1"
    assert evaluation.task_function_version_id == "fn-task-v2"
    assert evaluation.scorers[0].function_version_id == "fn-score-v2"
    assert evaluation.created_at is not None
    assert evaluation.created_at.isoformat() == "2026-08-14T18:22:00+00:00"
    assert connect.calls == [
        (
            "chalk.evaluation.v1.EvaluationService",
            "CreateEvaluation",
            {
                "name": "Support quality",
                "datasetId": "dataset-1",
                "datasetRevisionId": "revision-1",
                "taskFunctionVersionId": "fn-task-v2",
                "scorers": [{"function": {"functionVersionId": "fn-score-v2"}}],
                "metadata": {"suite": "release"},
            },
        )
    ]


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
        {"CreateEvaluation": [_FakeResponse(200, {"evaluation": response})]}
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
    request = connect.calls[0][2]
    assert request["datasetId"] == str(dataset_id)
    assert request["datasetRevisionId"] == str(revision_id)


def test_create_resolves_dataset_name_to_latest_revision() -> None:
    connect = _FakeConnectClient(
        {"CreateEvaluation": [_FakeResponse(200, {"evaluation": _evaluation_dict()})]}
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluation = client.create(
        "Latest goldens",
        dataset=DatasetRevisionRef(dataset_name="  support-goldens  "),
        task=_remote_function("fn-task-v2", name="support-answer"),
        scorers=[_remote_function("fn-score-v2", name="brand-alignment")],
    )

    assert evaluation.dataset == DatasetRevisionRef("dataset-1", "revision-1")
    request = connect.calls[0][2]
    assert request["datasetName"] == "support-goldens"
    assert "datasetId" not in request
    assert "datasetRevisionId" not in request


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
            "CreateEvaluation": [
                _FakeResponse(200, {"evaluation": _evaluation_dict()})
            ],
            "CreateEvaluationRun": [
                _FakeResponse(
                    200,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_PENDING")},
                )
            ],
            "GetEvaluationRun": [
                _FakeResponse(
                    200,
                    {"evaluationRun": _run_dict("EVALUATION_RUN_STATUS_RUNNING")},
                ),
                _FakeResponse(
                    200,
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
    assert [call[1] for call in connect.calls] == [
        "CreateEvaluation",
        "CreateEvaluationRun",
        "GetEvaluationRun",
        "GetEvaluationRun",
    ]
    assert connect.calls[1][2] == {
        "evaluationId": "eval-1",
        "metadata": {"git_sha": "abc123"},
    }


def test_wait_raises_typed_error_for_failed_run() -> None:
    connect = _FakeConnectClient(
        {
            "GetEvaluationRun": [
                _FakeResponse(
                    200,
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
        {
            "GetEvaluation": [
                _FakeResponse(
                    404,
                    {"code": "not_found", "message": "evaluation not found"},
                    text='{"code":"not_found","message":"evaluation not found"}',
                )
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(EvaluationNotFoundError, match="evaluation not found"):
        client.get("missing")


def test_list_evaluations_pages_and_honors_limit() -> None:
    second = _evaluation_dict()
    second["id"] = "eval-2"
    connect = _FakeConnectClient(
        {
            "ListEvaluations": [
                _FakeResponse(
                    200,
                    {
                        "evaluations": [_evaluation_dict()],
                        "nextCursor": "next-page",
                    },
                ),
                _FakeResponse(200, {"evaluations": [second]}),
            ]
        }
    )
    client = EvaluationClient(connect_client=connect)  # type: ignore[arg-type]

    evaluations = list(client.list(limit=2))

    assert [evaluation.id for evaluation in evaluations] == ["eval-1", "eval-2"]
    assert connect.calls[0][2] == {"limit": 2}
    assert connect.calls[1][2] == {"limit": 1, "cursor": "next-page"}


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
