from __future__ import annotations

import importlib
from pathlib import Path
import shutil
import sys

import pytest

from chalkcompute._function import RemoteFunction
from chalkcompute._function_source_location import function_source_location
from chalkcompute._function_source_location import FunctionSourceLocations
from chalkcompute._image import Image


def test_qualified_async_method_location():
    location = function_source_location(
        "# header\nclass Scorer:\n    @decorate\n    async def score(self):\n        pass\n",
        "/app/scorers.py",
        "Scorer.score",
    )
    assert location is not None
    assert (location.path, location.line_number, location.column_number) == (
        "/app/scorers.py",
        4,
        5,
    )


def test_class_locations_share_one_parse(monkeypatch):
    import ast
    from unittest.mock import Mock

    parse = Mock(wraps=ast.parse)
    monkeypatch.setattr(ast, "parse", parse)
    locations = FunctionSourceLocations(
        "class Scorer:\n    def first(self): pass\n    async def second(self): pass\n",
        "/app/scorers.py",
    )
    first = locations.get("Scorer.first")
    second = locations.get("Scorer.second")
    assert first is not None and second is not None
    assert (first.line_number, second.line_number) == (2, 3)
    assert (first.column_number, second.column_number) == (5, 5)
    assert locations.get("Scorer.missing") is None
    parse.assert_called_once()


@pytest.mark.parametrize(
    "source,name",
    [
        ("def other(): pass", "score"),
        ("def score(): pass\ndef score(): pass", "score"),
        ("class Scorer: pass", "Scorer"),
    ],
)
def test_unresolved_definitions_have_no_location(source, name):
    assert function_source_location(source, "/app/scorers.py", name) is None


@pytest.mark.parametrize("strip", ["0", "1"])
@pytest.mark.parametrize("module_file", ["scorers.py", "__init__.py"])
def test_packaged_definition_uses_captured_file_and_lines(
    tmp_path, monkeypatch, strip, module_file
):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", strip)
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_COPY", raising=False)
    package = tmp_path / "location_package"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / module_file).write_text(
        "# heading\n\nasync def score(x: int) -> int:\n    return x\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module_name = (
        "location_package"
        if module_file == "__init__.py"
        else "location_package.scorers"
    )
    module = importlib.import_module(module_name)
    fn = RemoteFunction(module.score, name="catalog-alias")
    temp_files = []
    try:
        image = fn._build_deploy_image()
        temp_files.extend(fn._temp_files)
        location = fn._source_location
        assert location is not None
        assert location.path == (
            "/app/user_module.py"
            if module_file == "__init__.py"
            else f"/app/location_package/{module_file}"
        )
        files = {
            file.dest: Path(file.src).read_text() for file in image.lazy_local_files
        }
        assert (
            files[location.path]
            .splitlines()[location.line_number - 1]
            .startswith("async def score(")
        )
        assert location.column_number == 1
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop("location_package", None)
        for path in temp_files:
            if Path(path).is_dir():
                shutil.rmtree(path)
            else:
                Path(path).unlink(missing_ok=True)


def test_notebook_definition_uses_bundled_lines(monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")

    def score(x: int) -> int:
        return x

    fn = RemoteFunction(score)
    fn._notebook_source = (
        "# bundled cells\n\n\ndef score(x: int) -> int:\n    return x\n"
    )
    temp_files = []
    try:
        fn._add_single_file_source(
            Image("python:3.11"), temp_files, "user_module", "/__chalk_source"
        )
        location = fn._source_location
        assert location is not None
        assert (location.path, location.line_number, location.column_number) == (
            "/__chalk_source/user_module.py",
            4,
            1,
        )
    finally:
        for path in temp_files:
            Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize("strip", ["0", "1"])
def test_class_method_uses_captured_lines(tmp_path, monkeypatch, strip):
    from chalkcompute._cls import RemoteClass
    import importlib.util

    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", strip)
    source = tmp_path / "location_class.py"
    source.write_text(
        "from chalkcompute import method\n\n"
        "class Scorer:\n    @method\n    def score(self, x: int) -> int:\n        return x\n"
        "    @method\n    async def other(self, x: int) -> int:\n        return x + 1\n"
    )
    spec = importlib.util.spec_from_file_location("location_class", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    remote_class = RemoteClass(module.Scorer)
    image, temporary = remote_class._build_deploy_image()
    try:
        location = remote_class._source_locations["score"]
        assert location is not None
        files = {
            file.dest: Path(file.src).read_text() for file in image.lazy_local_files
        }
        assert (
            files[location.path]
            .splitlines()[location.line_number - 1]
            .lstrip()
            .startswith("def score(")
        )
        assert location.column_number == 5
        other_location = remote_class._source_locations["other"]
        assert other_location is not None
        assert other_location.line_number > location.line_number
        assert (
            files[other_location.path]
            .splitlines()[other_location.line_number - 1]
            .lstrip()
            .startswith("async def other(")
        )
    finally:
        for path in temporary:
            Path(path).unlink(missing_ok=True)
