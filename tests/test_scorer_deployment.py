"""Exercise the files a fresh worker receives, without a remote deployment."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

from chalkcompute._function import _cleanup_temp_files

pytestmark = pytest.mark.usefixtures("no_deploy")
SDK_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def caller(tmp_path, monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_COPY", raising=False)
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_WHEEL", raising=False)

    def load(source, *, package=False, main=False):
        root = tmp_path / "local"
        root.mkdir(exist_ok=True)
        if package:
            root = root / "eval_package"
            root.mkdir(exist_ok=True)
            (root / "__init__.py").write_text("")
            (root / "config.py").write_text("MODEL = 'test-model'\n")
        path = root / "evaluation.py"
        path.write_text(textwrap.dedent(source))
        name = "__main__" if main else "evaluation"
        if package:
            name = "eval_package.evaluation"
        monkeypatch.syspath_prepend(str(tmp_path / "local"))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    yield load
    sys.modules.pop("eval_package", None)
    sys.modules.pop("eval_package.config", None)


def worker(scorer, tmp_path, *, columns, setup=""):
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    image = scorer._build_deploy_image()
    try:
        for file in image.lazy_local_files:
            destination = remote / file.dest.removeprefix("/app/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file.src, destination)
        handler = (remote / f"{scorer._handler_module}.py").read_text()
        # Read the real shim's import, so accidentally leaving it pointed at
        # user_module makes these tests fail just like worker startup would.
        entrypoint = next(
            line.strip()
            for line in handler.splitlines()
            if line.strip().endswith(f" import {scorer._func_name}")
        )
        code = (
            setup
            + "\n"
            + entrypoint
            + " as score\n"
            + textwrap.dedent(
                f"""
            import inspect, json, typing
            from chalkcompute._function import _build_schemas
            result = score(**{columns!r})
            _, output_schema = _build_schemas(score)
            print(json.dumps({{
                'score': result.score,
                'metadata': result.metadata,
                'parameters': list(inspect.signature(score).parameters),
                'schema': dict(output_schema.metadata),
            }}))
            """
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=remote,
            env={
                **os.environ,
                "PYTHONPATH": str(SDK_ROOT),
                "CHALK_REMOTE_FUNCTION": "1",
                "CHALK_FUNCTION_STRIP_SOURCE": "0",
                "OPENAI_API_KEY": "test-key",
            },
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    finally:
        _cleanup_temp_files(scorer._temp_files)


@pytest.mark.parametrize(
    "expression,columns,expected",
    [
        ("exact_match(case_insensitive=True)", {"output": "YES", "expected": "yes"}, 1),
        ("levenshtein()", {"output": "cat", "expected": "cut"}, 2 / 3),
        ("regex_match(r'(?P<word>yes)', flags=2)", {"output": "YES"}, 1),
        (
            "contains(mode='fraction')",
            {"output": "cat", "expected": '["cat", "dog"]'},
            0.5,
        ),
        ("numeric_close(tolerance=2)", {"output": "10", "expected": "11"}, 1),
        (
            "string_similarity('jaccard')",
            {"output": "a cat", "expected": "a dog"},
            2 / 7,
        ),
        ("json_valid()", {"output": '{"a": 1}'}, 1),
        (
            "json_match(paths=['a'])",
            {"output": '{"a": 1, "b": 2}', "expected": '{"a": 1}'},
            1,
        ),
        ("embedding_similarity()", {"output": "", "expected": ""}, 1),
        (
            "jev(labels={'Y': 1, 'N': 0}, rubric='Y is correct', model='cheap', fallback_model='strong')",
            {"output": "yes"},
            1,
        ),
    ],
)
def test_scorer_factories_inside_setup_load_in_fresh_worker(
    caller, tmp_path, expression, columns, expected
):
    module = caller(f"""
        from chalkcompute import scorers
        def create_evaluation():
            if __import__('os').environ.get('CHALK_REMOTE_FUNCTION'):
                raise AssertionError('worker must not replay evaluation setup')
            return scorers.{expression}
    """)
    scorer = module.create_evaluation()
    setup = """
from chalkcompute import scorers
def complete(spec, messages):
    return spec.response_model(choice='Y', rationale='correct', probabilities=[
        {'label': 'Y', 'probability': 0.99}, {'label': 'N', 'probability': 0.01}])
scorers._complete = complete
"""
    result = worker(scorer, tmp_path, columns=columns, setup=setup)
    assert result["score"] == pytest.approx(expected)
    assert result["parameters"] == list(columns)


@pytest.mark.parametrize("layout", ["script", "package", "main"])
@pytest.mark.parametrize("model_kind", ["dataclass", "pydantic"])
def test_inline_judge_captures_configuration_and_imports_user_hooks(
    caller, tmp_path, layout, model_kind
):
    source = """
import json
from dataclasses import dataclass
from pydantic import BaseModel, Field
from chalkcompute import scorers

MODEL = 'test-model'

@dataclass
class Grade:
    score: float
    reason: str

def prompt(model, *, output, trace):
    assert model is Grade
    assert trace is None
    return 'custom prompt: ' + output

def parse(text):
    return Grade(**json.loads(text))

def create_evaluation(instructions):
    if __import__('os').environ.get('CHALK_REMOTE_FUNCTION'):
        raise AssertionError('worker must not replay evaluation setup')
    return scorers.llm_judge(Grade, model=MODEL, instructions=instructions,
        inputs=('output', 'trace'), prompt_fn=prompt, parse_fn=parse)
"""
    if model_kind == "pydantic":
        source = source.replace("@dataclass\nclass Grade:", "class Grade(BaseModel):")
        source = source.replace("score: float\n", "score: float = Field(ge=0, le=1)\n")
    if layout == "package":
        source = source.replace("MODEL = 'test-model'", "from .config import MODEL")
    if layout == "main":
        source += "\nif __name__ == '__main__':\n    judge = create_evaluation('Main only.')\n"
    module = caller(source, package=layout == "package", main=layout == "main")
    # Same generated Python name, different captured configurations. The second
    # declaration overwrites the caller's symbol but must not change the first.
    first = module.create_evaluation("Be strict.")
    second = module.create_evaluation("Be lenient.")
    setup = """
import sys, types
def create(**request):
    assert request['model'] == 'test-model'
    assert request['messages'][1]['content'] == 'custom prompt: answer'
    reason = request['messages'][0]['content']
    import json
    message = types.SimpleNamespace(content=json.dumps({'score': 0.75, 'reason': reason}), refusal=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason='stop')])
sys.modules['openai'] = types.SimpleNamespace(OpenAI=lambda **kwargs:
    types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))))
"""
    for scorer, instructions in [(first, "Be strict."), (second, "Be lenient.")]:
        result = worker(
            scorer, tmp_path, columns={"output": "answer", "trace": None}, setup=setup
        )
        assert result["score"] == 0.75
        assert result["metadata"] == {"reason": instructions}
        assert result["parameters"] == ["output", "trace"]
        schema = json.loads(result["schema"]["chalk.scorer.response_schema"])
        assert list(schema["properties"]) == ["reason"]


def test_unimportable_inline_judge_configuration_has_actionable_error(caller, tmp_path):
    module = caller("""
        from dataclasses import dataclass
        from chalkcompute import scorers
        def create_evaluation():
            @dataclass
            class Grade:
                score: float
            return scorers.llm_judge(Grade, model='test-model')
    """)
    with pytest.raises(AssertionError, match="importable module"):
        worker(module.create_evaluation(), tmp_path, columns={"output": "answer"})


def test_import_time_judge_with_local_model_and_lambda_still_loads(caller, tmp_path):
    module = caller("""
        from dataclasses import dataclass
        from chalkcompute import scorers
        def create_evaluation():
            @dataclass
            class Grade:
                score: float
            return scorers.llm_judge(Grade, model='test-model',
                prompt_fn=lambda model, **columns: 'custom prompt')
        judge = create_evaluation()
    """)
    result = worker(
        module.judge,
        tmp_path,
        columns={"output": "answer"},
        setup="""
from chalkcompute import scorers
def complete(spec, messages):
    assert messages[-1]['content'] == 'custom prompt'
    return {'score': 0.5}
scorers._complete = complete
""",
    )
    assert result["score"] == 0.5


def test_generated_scorer_sources_are_stable_and_avoid_image_modules(caller, tmp_path):
    module = caller("""
        from chalkcompute import scorers
        def create_evaluation(image):
            return scorers.exact_match(image=image)
    """)
    from chalkcompute import Image

    user_source = tmp_path / "_chalk_scorer.py"
    user_source.write_text("# user-owned module\n")
    image = Image.debian_slim().add_local_file(
        str(user_source), "/app/_chalk_scorer.py"
    )
    sources = []
    for _ in range(2):
        scorer = module.create_evaluation(image)
        built = scorer._build_deploy_image()
        try:
            sources.append(
                {f.dest: Path(f.src).read_text() for f in built.lazy_local_files}
            )
            assert sources[-1]["/app/_chalk_scorer.py"] == "# user-owned module\n"
            assert "/app/_chalk_scorer_2.py" in sources[-1]
        finally:
            _cleanup_temp_files(scorer._temp_files)
    assert sources[0] == sources[1]
