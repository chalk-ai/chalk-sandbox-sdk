from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

import chalkcompute
from chalkcompute._containerspec import ComputeClass
from chalkcompute._experiment import (
    ExperimentClient,
    ExperimentError,
    ExperimentFailedError,
    ExperimentNotFoundError,
    ExperimentPhase,
    ExperimentStatus,
)
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.evaluation.v1 import experiment_pb
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


def _client(responses: Mapping[str, list[Any]]) -> tuple[ExperimentClient, Any]:
    connect = _FakeConnectClient(responses)
    return ExperimentClient(connect_client=connect), connect  # type: ignore[arg-type]


def _experiment_dict(status: str = "EXPERIMENT_STATUS_RUNNING", **extra: Any) -> dict:
    return {
        "id": "exp-1",
        "evaluationId": "eval-1",
        "status": status,
        "config": {
            "baseModel": "Qwen/Qwen3-4B-Instruct-2507",
            "iterations": 2,
            "samplesPerRow": 4,
            "rewardWeights": {"scorer-b": 1.0, "scorer-a": 0.5},
            "trainerGpu": "nvidia-l4:1",
            "modelRegistryName": "support-quality-post-train",
        },
        "createdBy": "user-1",
        "createdAt": "2026-11-19T08:38:37Z",
        **extra,
    }


def _response(message_type: type[Any], **experiment: Any) -> Any:
    return _message(message_type, {"experiment": _experiment_dict(**experiment)})


def _evaluation_response() -> Any:
    return _message(
        evaluation_pb.GetEvaluationResponse,
        {
            "evaluation": {
                "id": "eval-1",
                "name": "Support quality",
                "datasetId": "dataset-1",
                "datasetRevisionId": "revision-1",
                "taskFunctionVersionId": "fn-task-v2",
                "scorers": [
                    {
                        "id": "scorer-a",
                        "spec": {"function": {"functionVersionId": "a"}},
                    },
                    {
                        "id": "scorer-b",
                        "spec": {"function": {"functionVersionId": "b"}},
                    },
                ],
            }
        },
    )


def test_experiment_is_exported_from_the_package() -> None:
    assert chalkcompute.Experiment.__module__ == "chalkcompute._experiment"


def test_start_sends_config_and_leaves_server_defaults_unset() -> None:
    client, connect = _client(
        {"start_experiment": [_response(experiment_pb.StartExperimentResponse)]}
    )

    experiment = client.start(
        "eval-1",
        base_model=" Qwen/Qwen3-4B-Instruct-2507 ",
        gpu="nvidia-l4:1",
        steps=2,
        samples_per_row=4,
        reward_weights={"scorer-a": 1, "scorer-b": -0.5},
        register_as="support-quality",
        recipe={"method": "grpo"},
        compute_class=ComputeClass.HOST,
    )

    [(method, request)] = connect.calls
    assert method == "start_experiment"
    assert request.evaluation_id == "eval-1"
    config = request.config
    assert config.base_model == "Qwen/Qwen3-4B-Instruct-2507"
    assert config.trainer_gpu == "nvidia-l4:1"
    assert (config.iterations, config.samples_per_row) == (2, 4)
    assert dict(config.reward_weights) == {"scorer-a": 1.0, "scorer-b": -0.5}
    assert config.model_registry_name == "support-quality"
    assert config.trainer_compute_class == container_pb.ComputeClass.HOST
    assert json.loads(config.recipe.to_json()) == {"method": "grpo"}
    # Zero and empty are what the server replaces with its own defaults.
    assert (config.learning_rate, config.lora_rank, config.trainer_image) == (0, 0, "")
    assert experiment.id == "exp-1"
    assert experiment.status is ExperimentStatus.RUNNING
    assert experiment.register_as == "support-quality-post-train"


def test_start_weights_every_scorer_of_the_evaluation_by_default() -> None:
    client, connect = _client(
        {
            "get_evaluation": [_evaluation_response()],
            "start_experiment": [_response(experiment_pb.StartExperimentResponse)],
        }
    )

    client.start(
        "eval-1",
        base_model="Qwen/Qwen3-4B-Instruct-2507",
        gpu="nvidia-l4:1",
        steps=1,
        samples_per_row=2,
    )

    assert [method for method, _ in connect.calls] == [
        "get_evaluation",
        "start_experiment",
    ]
    start_request = connect.calls[1][1]
    assert dict(start_request.config.reward_weights) == {
        "scorer-a": 1.0,
        "scorer-b": 1.0,
    }
    assert not start_request.config.has_field("model_registry_name")
    assert not start_request.config.has_field("trainer_compute_class")


def test_start_takes_an_evaluation_handle_without_fetching_it() -> None:
    client, connect = _client(
        {
            "get_evaluation": [_evaluation_response()],
            "start_experiment": [_response(experiment_pb.StartExperimentResponse)],
        }
    )
    evaluation = client.evaluations.get("eval-1")
    connect.calls.clear()

    client.start(
        evaluation,
        base_model="Qwen/Qwen3-4B-Instruct-2507",
        gpu="nvidia-l4:1",
        steps=1,
        samples_per_row=2,
    )

    [(method, request)] = connect.calls
    assert method == "start_experiment"
    assert request.evaluation_id == "eval-1"
    assert sorted(request.config.reward_weights) == ["scorer-a", "scorer-b"]


def test_classmethods_reuse_the_client_an_evaluation_handle_was_loaded_with() -> None:
    client, connect = _client(
        {
            "get_evaluation": [_evaluation_response()],
            "start_experiment": [_response(experiment_pb.StartExperimentResponse)],
            "list_experiments": [
                _message(
                    experiment_pb.ListExperimentsResponse,
                    {"experiments": [_experiment_dict()]},
                )
            ],
        }
    )
    evaluation = client.evaluations.get("eval-1")

    # No chalk_client: an ambient client would need real credentials here.
    started = chalkcompute.Experiment.start(
        evaluation,
        base_model="Qwen/Qwen3-4B-Instruct-2507",
        gpu="nvidia-l4:1",
        steps=1,
        samples_per_row=2,
    )
    listed = chalkcompute.Experiment.list_all(evaluation=evaluation)

    assert [method for method, _ in connect.calls] == [
        "get_evaluation",
        "start_experiment",
        "list_experiments",
    ]
    assert started.id == "exp-1"
    assert [experiment.id for experiment in listed] == ["exp-1"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base_model": " "}, "base_model is required"),
        ({"gpu": ""}, "gpu is required"),
        ({"reward_weights": {}}, "at least one scorer"),
        ({"reward_weights": {"scorer-a": float("nan")}}, "must be finite"),
        ({"recipe": {"bad": object()}}, "recipe must be JSON serializable"),
    ],
)
def test_start_rejects_invalid_arguments_without_rpc(
    overrides: dict[str, Any], message: str
) -> None:
    client, connect = _client({})
    arguments: dict[str, Any] = {
        "base_model": "Qwen/Qwen3-4B-Instruct-2507",
        "gpu": "nvidia-l4:1",
        "steps": 1,
        "samples_per_row": 2,
        "reward_weights": {"scorer-a": 1.0},
        **overrides,
    }

    with pytest.raises(ValueError, match=message):
        client.start("eval-1", **arguments)

    assert connect.calls == []


def test_result_compares_the_final_run_with_the_pinned_baseline() -> None:
    client, _ = _client(
        {
            "get_experiment": [
                _response(
                    experiment_pb.GetExperimentResponse,
                    status="EXPERIMENT_STATUS_SUCCEEDED",
                    baselineRunId="run-baseline",
                    trainingRunId="training-1",
                    modelVersionId="version-3",
                    finalRunId="run-final",
                    baselineScorerScores={"scorer-a": 0.25, "scorer-b": 0.5},
                    finalScorerScores={"scorer-a": 0.75, "scorer-b": 0.4},
                    iterations=[
                        {
                            "phase": "EXPERIMENT_PHASE_DONE",
                            "policyModel": "posttrain/Qwen/Qwen3-4B-Instruct-2507",
                            "evaluationRunIds": ["run-1", "run-2"],
                            "trainingRunId": "training-1",
                            "meanReward": 0.45,
                            "scorerAverageScores": {"scorer-a": 0.3},
                        },
                        {"iteration": 1, "phase": "EXPERIMENT_PHASE_ROLLOUT"},
                    ],
                )
            ]
        }
    )

    experiment = client.get("exp-1")

    assert experiment.is_terminal
    assert experiment.baseline_run_id == "run-baseline"
    assert experiment.final_run_id == "run-final"
    assert experiment.model_version_id == "version-3"
    assert experiment.training_run is not None
    assert experiment.training_run.run_id == "training-1"
    # Scorers come in the config's reward-weight order.
    assert [
        (row.scorer_id, row.baseline, row.trained, row.change)
        for row in experiment.result.scorers
    ] == [
        ("scorer-b", 0.5, 0.4, pytest.approx(-0.1)),
        ("scorer-a", 0.25, 0.75, pytest.approx(0.5)),
    ]
    assert experiment.result.table().splitlines() == [
        "scorer   | baseline | trained | change",
        "scorer-b |    0.500 |   0.400 | -0.100",
        "scorer-a |    0.250 |   0.750 | +0.500",
    ]
    first, second = experiment.steps
    assert (first.index, first.phase, first.mean_reward) == (
        0,
        ExperimentPhase.DONE,
        0.45,
    )
    assert first.evaluation_run_ids == ("run-1", "run-2")
    assert first.scorer_scores == {"scorer-a": 0.3}
    assert (second.index, second.phase, second.mean_reward) == (
        1,
        ExperimentPhase.ROLLOUT,
        None,
    )


def test_result_has_no_trained_score_before_the_final_run() -> None:
    client, _ = _client(
        {
            "get_experiment": [
                _response(
                    experiment_pb.GetExperimentResponse,
                    baselineScorerScores={"scorer-a": 0.25},
                )
            ]
        }
    )

    experiment = client.get("exp-1")

    assert experiment.baseline_run is None
    assert experiment.final_run is None
    assert experiment.training_run is None
    by_scorer = {row.scorer_id: row for row in experiment.result.scorers}
    assert (by_scorer["scorer-a"].trained, by_scorer["scorer-a"].change) == (None, None)
    assert by_scorer["scorer-b"].baseline is None


def test_baseline_and_final_runs_resolve_through_the_evaluation_client() -> None:
    client, connect = _client(
        {
            "get_experiment": [
                _response(experiment_pb.GetExperimentResponse, baselineRunId="run-9")
            ],
            "get_evaluation_run": [
                _message(
                    evaluation_pb.GetEvaluationRunResponse,
                    {
                        "evaluationRun": {
                            "id": "run-9",
                            "evaluationId": "eval-1",
                            "status": "EVALUATION_RUN_STATUS_SUCCEEDED",
                        }
                    },
                )
            ],
        }
    )

    run = client.get("exp-1").baseline_run

    assert run is not None and run.id == "run-9"
    assert connect.calls[-1][1].evaluation_run_id == "run-9"


def test_wait_polls_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chalkcompute._experiment.time.sleep", lambda _seconds: None)
    client, connect = _client(
        {
            "get_experiment": [
                _response(experiment_pb.GetExperimentResponse),
                _response(experiment_pb.GetExperimentResponse),
                _response(
                    experiment_pb.GetExperimentResponse,
                    status="EXPERIMENT_STATUS_SUCCEEDED",
                    finalRunId="run-final",
                ),
            ]
        }
    )

    finished = client.get("exp-1").wait()

    assert finished.status is ExperimentStatus.SUCCEEDED
    assert len(connect.calls) == 3


def test_wait_raises_for_a_success_that_has_no_final_run() -> None:
    client, _ = _client(
        {
            "get_experiment": [
                _response(
                    experiment_pb.GetExperimentResponse,
                    status="EXPERIMENT_STATUS_SUCCEEDED",
                    modelVersionId="version-3",
                )
            ]
        }
    )
    experiment = client.get("exp-1")

    with pytest.raises(ExperimentError, match="experiments_enabled") as raised:
        experiment.wait()

    assert not isinstance(raised.value, ExperimentFailedError)
    assert experiment.wait(raise_on_failure=False) is experiment


@pytest.mark.parametrize(
    ("status", "error_message", "match"),
    [
        ("EXPERIMENT_STATUS_FAILED", "no GPU was available", "no GPU was available"),
        ("EXPERIMENT_STATUS_CANCELED", None, "entered canceled state"),
    ],
)
def test_wait_raises_typed_error_unless_the_experiment_succeeded(
    status: str, error_message: str | None, match: str
) -> None:
    extra = {} if error_message is None else {"errorMessage": error_message}
    client, _ = _client(
        {
            "get_experiment": [
                _response(experiment_pb.GetExperimentResponse, status=status, **extra)
            ]
        }
    )
    experiment = client.get("exp-1")

    with pytest.raises(ExperimentFailedError, match=match) as raised:
        experiment.wait()

    assert raised.value.experiment is experiment
    assert experiment.wait(raise_on_failure=False) is experiment


def test_wait_times_out_on_an_unfinished_experiment() -> None:
    client, _ = _client(
        {"get_experiment": [_response(experiment_pb.GetExperimentResponse)]}
    )

    with pytest.raises(TimeoutError, match="did not finish within 0s"):
        client.get("exp-1").wait(timeout=0)


def test_list_filters_by_evaluation() -> None:
    client, connect = _client(
        {
            "list_experiments": [
                _message(
                    experiment_pb.ListExperimentsResponse,
                    {"experiments": [_experiment_dict(), _experiment_dict(id="exp-2")]},
                ),
                _message(experiment_pb.ListExperimentsResponse, {}),
            ]
        }
    )

    experiments = client.list(evaluation="eval-1")
    everything = client.list()

    assert [experiment.id for experiment in experiments] == ["exp-1", "exp-2"]
    assert everything == []
    assert connect.calls[0][1].filters.evaluation_id == "eval-1"
    assert not connect.calls[1][1].filters.has_field("evaluation_id")


def test_cancel_returns_the_canceled_experiment() -> None:
    client, connect = _client(
        {
            "get_experiment": [_response(experiment_pb.GetExperimentResponse)],
            "cancel_experiment": [
                _response(
                    experiment_pb.CancelExperimentResponse,
                    status="EXPERIMENT_STATUS_CANCELED",
                )
            ],
        }
    )

    canceled = client.get("exp-1").cancel()

    assert canceled.status is ExperimentStatus.CANCELED
    assert connect.calls[-1][1].experiment_id == "exp-1"


def test_retry_without_arguments_keeps_the_config() -> None:
    client, connect = _client(
        {
            "get_experiment": [
                _response(
                    experiment_pb.GetExperimentResponse,
                    status="EXPERIMENT_STATUS_FAILED",
                )
            ],
            "retry_experiment": [
                _response(experiment_pb.RetryExperimentResponse, retryCount=1)
            ],
        }
    )

    retried = client.get("exp-1").retry()

    request = connect.calls[-1][1]
    assert request.experiment_id == "exp-1"
    assert request.config is None
    assert request.update_mask is None
    assert retried.retry_count == 1


def test_retry_masks_exactly_the_replaced_settings() -> None:
    client, connect = _client(
        {"retry_experiment": [_response(experiment_pb.RetryExperimentResponse)]}
    )

    client.retry("exp-1", gpu="nvidia-l40s:1", steps=3, learning_rate=2e-5)

    request = connect.calls[-1][1]
    assert list(request.update_mask.paths) == [
        "trainer_gpu",
        "iterations",
        "learning_rate",
    ]
    assert request.config.trainer_gpu == "nvidia-l40s:1"
    assert request.config.iterations == 3
    assert request.config.learning_rate == 2e-5


def test_get_maps_not_found_to_typed_error() -> None:
    client, _ = _client(
        {"get_experiment": [ConnectRpcError(Code.NOT_FOUND, "experiment not found")]}
    )

    with pytest.raises(ExperimentNotFoundError, match="experiment not found") as raised:
        client.get("missing")

    assert raised.value.status_code == 404


def test_other_rpc_failures_keep_their_status_code() -> None:
    client, _ = _client(
        {
            "start_experiment": [
                ConnectRpcError(
                    Code.FAILED_PRECONDITION,
                    "the environment has no cluster to serve the experiment policy from",
                )
            ]
        }
    )

    with pytest.raises(ExperimentError, match="no cluster") as raised:
        client.start(
            "eval-1",
            base_model="Qwen/Qwen3-4B-Instruct-2507",
            gpu="nvidia-l4:1",
            steps=1,
            samples_per_row=2,
            reward_weights={"scorer-a": 1.0},
        )

    assert not isinstance(raised.value, ExperimentNotFoundError)
