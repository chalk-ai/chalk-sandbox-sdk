"""RemoteFunction.deploy() warns before taking over a scaling group it doesn't own."""

import logging

import pytest
from protobuf.wkt import struct_pb

from chalkcompute._function import FunctionError, _warn_if_scaling_group_taken
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb


def _sg(
    status: str = "Available", ready: int = 1, **metadata: str
) -> sg_pb.ScalingGroupResponse:
    return sg_pb.ScalingGroupResponse(
        name="add",
        status=status,
        ready_replicas=ready,
        available_replicas=ready,
        metadata={k: struct_pb.Value.from_python(v) for k, v in metadata.items()},
    )


@pytest.fixture
def probe(monkeypatch):
    """Pin what the GetScalingGroup probe sees; returns a setter."""

    def _set(result):
        def fake(client, name):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr("chalkcompute._function._get_scaling_group", fake)

    return _set


def test_warns_when_another_function_holds_the_name(probe, caplog):
    probe(_sg(function_name="other_fn"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert "function 'other_fn'" in caplog.text
    assert "serving traffic" in caplog.text


def test_warns_when_a_model_holds_the_name(probe, caplog):
    probe(_sg(model_name="house_price", model_version="3"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert "model 'house_price'" in caplog.text


def test_warns_when_a_standalone_group_holds_the_name(probe, caplog):
    probe(_sg())
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert "standalone scaling group" in caplog.text


def test_reports_status_when_not_serving_traffic(probe, caplog):
    probe(_sg(status="Error", ready=0, function_name="other_fn"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert "has status Error" in caplog.text
    assert "serving traffic" not in caplog.text


def test_silent_on_redeploy_of_the_same_function(probe, caplog):
    probe(_sg(function_name="add"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert caplog.text == ""


def test_silent_when_the_name_is_free(probe, caplog):
    probe(FunctionError("GetScalingGroup failed: not_found"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert caplog.text == ""


def test_never_raises_when_the_probe_blows_up(probe, caplog):
    probe(RuntimeError("unauthenticated"))
    with caplog.at_level(logging.WARNING, logger="chalkcompute._function"):
        _warn_if_scaling_group_taken(None, "add", "add")
    assert caplog.text == ""
