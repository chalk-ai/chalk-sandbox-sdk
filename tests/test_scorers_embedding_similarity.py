"""Tests for ``chalkcompute.scorers.embedding_similarity``."""

from __future__ import annotations

import sys
import types

import pytest

from chalkcompute import scorers
from chalkcompute._secret import Secret

pytestmark = pytest.mark.usefixtures("no_deploy")


class _FakeEmbeddings:
    """Stands in for ``client.embeddings``, returning canned vectors."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.requests: list[dict] = []

    def create(self, **request):
        self.requests.append(request)
        data = [types.SimpleNamespace(embedding=vector) for vector in self.vectors]
        return types.SimpleNamespace(data=data)


@pytest.fixture
def embeddings(monkeypatch: pytest.MonkeyPatch):
    """A fake OpenAI client whose vectors the test sets."""
    state: dict = {"vectors": [[1.0, 0.0], [1.0, 0.0]], "options": None}

    def fake_client(**options):
        state["options"] = options
        state["calls"] = _FakeEmbeddings(state["vectors"])
        return types.SimpleNamespace(embeddings=state["calls"])

    monkeypatch.setitem(
        sys.modules, "openai", types.SimpleNamespace(OpenAI=fake_client)
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("CHALK_ACTIVE_ENVIRONMENT", raising=False)
    return state


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


def test_scores_the_cosine_between_the_two_columns(embeddings) -> None:
    embeddings["vectors"] = [[1.0, 0.0], [1.0, 0.0]]
    scorer = scorers.embedding_similarity()

    graded = _score(scorer, "a cat sat down", "the cat sat")

    assert graded.score == pytest.approx(1.0)
    assert graded.metadata == {"cosine": pytest.approx(1.0)}


def test_unrelated_and_opposite_vectors(embeddings) -> None:
    embeddings["vectors"] = [[1.0, 0.0], [0.0, 1.0]]
    assert _score(scorers.embedding_similarity(), "cat", "tax law").score == 0.0

    # A negative cosine clips to zero, and the raw value is still reported.
    embeddings["vectors"] = [[1.0, 0.0], [-1.0, 0.0]]
    graded = _score(scorers.embedding_similarity(), "yes", "no")
    assert graded.score == 0.0
    assert graded.metadata["cosine"] == pytest.approx(-1.0)


def test_asks_for_both_columns_in_one_request(embeddings) -> None:
    scorer = scorers.embedding_similarity(model="text-embedding-3-large")

    _score(scorer, "first", "second")

    assert embeddings["calls"].requests == [
        {"model": "text-embedding-3-large", "input": ["first", "second"]}
    ]


def test_empty_columns_never_reach_the_model(embeddings) -> None:
    scorer = scorers.embedding_similarity()

    assert _score(scorer, "", "").score == 1.0
    assert _score(scorer, "", "something").score == 0.0
    assert _score(scorer, None, None).score == 1.0
    assert embeddings["options"] is None


def test_the_endpoint_and_its_secret_are_passed_through(embeddings) -> None:
    secret = Secret.from_chalk_env("VOYAGE_KEY")
    scorer = scorers.embedding_similarity(
        base_url="https://api.voyageai.com/v1", api_key=secret
    )
    import os

    os.environ["VOYAGE_KEY"] = "vk-test"
    try:
        _score(scorer, "a", "b")
    finally:
        del os.environ["VOYAGE_KEY"]

    assert secret in scorer._secrets
    assert embeddings["options"]["base_url"] == "https://api.voyageai.com/v1"
    assert embeddings["options"]["api_key"] == "vk-test"


def test_the_image_carries_the_openai_client() -> None:
    scorer = scorers.embedding_similarity()

    installed = [
        package
        for step in scorer._image._steps
        if step.step.field == "pip_install"
        for package in step.step.value.packages
    ]
    assert installed == ["openai"]


def test_cosine_of_a_zero_vector_is_zero() -> None:
    assert scorers._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_rejects_inputs_that_are_not_a_pair() -> None:
    assert scorers.embedding_similarity()._explicit_name == "embedding-similarity"
    with pytest.raises(ValueError, match="candidate column"):
        scorers.embedding_similarity(inputs=("output",))
