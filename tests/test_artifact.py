from __future__ import annotations

import pytest

from chalkcompute._artifact import (
    Artifact,
    ArtifactError,
    _artifact_from_proto,
)
from chalkcompute._gen.chalk.artifactstore.v1 import service_pb2 as pb
from chalkcompute._sandbox import PythonExecution


def _chart_proto() -> pb.Artifact:
    return pb.Artifact(
        id="art_1",
        name="fig",
        kind=pb.ARTIFACT_KIND_CHART,
        producer=pb.ARTIFACT_PRODUCER_SYSTEM,
        content_type="application/json",
        inline=pb.ArtifactInlineContent(data=b'{"$schema": "vega-lite"}'),
        origin=pb.ArtifactOrigin(
            sandbox_exec=pb.SandboxExecOrigin(sandbox_id="sb_1", execution_id="ex_1")
        ),
        chart=pb.ChartDetails(
            vega_lite=pb.VegaLiteChartDetails(source="altair"),
            variable_name="fig",
        ),
    )


def test_artifact_from_proto_inline_chart() -> None:
    artifact = _artifact_from_proto(_chart_proto(), client=None)
    assert artifact.id == "art_1"
    assert artifact.kind == "chart"
    assert artifact.producer == "system"
    assert artifact.chart_encoding == "vega_lite"
    assert artifact.chart_variable_name == "fig"
    assert artifact.sandbox_id == "sb_1"
    assert artifact.execution_id == "ex_1"
    assert artifact.data == b'{"$schema": "vega-lite"}'
    assert artifact.byte_size == len(artifact.data)
    # Inline content downloads without a client.
    assert artifact.download() == artifact.data


def test_artifact_from_proto_blob_backed() -> None:
    msg = pb.Artifact(
        id="art_2",
        name="report.html",
        kind=pb.ARTIFACT_KIND_HTML,
        producer=pb.ARTIFACT_PRODUCER_USER,
        content_type="text/html",
        blob=pb.ArtifactBlobRef(byte_size=123, content_sha256="abc"),
    )
    artifact = _artifact_from_proto(msg, client=None)
    assert artifact.kind == "html"
    assert artifact.producer == "user"
    assert artifact.data is None
    assert artifact.byte_size == 123
    with pytest.raises(ArtifactError):
        artifact.download()


def test_python_execution_charts_filter() -> None:
    chart = _artifact_from_proto(_chart_proto(), client=None)
    html = Artifact(
        id="art_3",
        name="page",
        kind="html",
        producer="user",
        content_type="text/html",
        byte_size=1,
        data=b"x",
    )
    execution = PythonExecution(
        execution_id="ex_1",
        stdout=b"hello\n",
        stderr=b"",
        exit_code=0,
        outcome="succeeded",
        artifacts=[chart, html],
    )
    assert execution.succeeded
    assert execution.stdout_text == "hello\n"
    assert execution.charts == [chart]
