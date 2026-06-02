"""Unit tests for Image construction, exclude patterns, and .chalkignore support.

These tests run without any network access or credentials.
"""

import tempfile
from pathlib import Path

import pytest

from chalkcompute import Image


# ---------------------------------------------------------------------------
# Image constructors
# ---------------------------------------------------------------------------


class TestImageBase:
    """Test Image.base() constructor."""

    def test_base_creates_image_with_given_ref(self) -> None:
        img = Image.base("ubuntu:22.04")
        spec = img.to_proto()
        assert spec.base_image == "ubuntu:22.04"
        assert len(spec.steps) == 0

    def test_base_arbitrary_registry(self) -> None:
        img = Image.base("ghcr.io/org/custom-image:v1.2.3")
        assert img.to_proto().base_image == "ghcr.io/org/custom-image:v1.2.3"


class TestImageDebianSlim:
    """Test Image.debian_slim() constructor."""

    def test_default_python_version(self) -> None:
        img = Image.debian_slim()
        assert img.to_proto().base_image == "python:3.14-slim-trixie"

    def test_custom_python_version(self) -> None:
        img = Image.debian_slim("3.14.5")
        assert img.to_proto().base_image == "python:3.14.5-slim-trixie"


class TestImageFromDockerfile:
    """Test Image.from_dockerfile() constructor."""

    def test_simple_dockerfile(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Dockerfile", delete=False
        ) as f:
            f.write("FROM python:3.14\nRUN pip install requests\nEXPOSE 8080\n")
            f.flush()
            img = Image.from_dockerfile(f.name)
        spec = img.to_proto()
        assert spec.base_image == "python:3.14"
        assert len(spec.steps) == 1
        assert spec.steps[0].dockerfile_commands.commands == [
            "RUN pip install requests",
            "EXPOSE 8080",
        ]

    def test_dockerfile_with_comments(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Dockerfile", delete=False
        ) as f:
            f.write(
                "# Build stage\nFROM alpine:3.23\n# Install deps\nRUN apk add git\n"
            )
            f.flush()
            img = Image.from_dockerfile(f.name)
        spec = img.to_proto()
        assert spec.base_image == "alpine:3.23"
        assert spec.steps[0].dockerfile_commands.commands == ["RUN apk add git"]

    def test_dockerfile_chaining(self) -> None:
        """from_dockerfile images can be further extended."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Dockerfile", delete=False
        ) as f:
            f.write("FROM python:3.14\nRUN echo base\n")
            f.flush()
            img = Image.from_dockerfile(f.name).pip_install(["numpy"])
        spec = img.to_proto()
        assert len(spec.steps) == 2
        assert spec.steps[1].pip_install.packages == ["numpy"]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImageImmutability:
    """Verify builder methods return new instances without mutating the original."""

    def test_run_commands_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.run_commands("echo hello")
        assert len(base.to_proto().steps) == 0
        assert len(extended.to_proto().steps) == 1

    def test_pip_install_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.pip_install(["requests"])
        assert len(base.to_proto().steps) == 0
        assert len(extended.to_proto().steps) == 1

    def test_env_does_not_mutate(self) -> None:
        base = Image.debian_slim().env({"A": "1"})
        extended = base.env({"B": "2"})
        assert "B" not in base.to_proto().env
        assert extended.to_proto().env["A"] == "1"
        assert extended.to_proto().env["B"] == "2"

    def test_workdir_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.workdir("/app")
        assert not base.to_proto().HasField("workdir")
        assert extended.to_proto().workdir == "/app"

    def test_entrypoint_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.entrypoint(["/bin/bash"])
        assert base.to_proto().entrypoint == []
        assert extended.to_proto().entrypoint == ["/bin/bash"]

    def test_cmd_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.cmd(["--help"])
        assert base.to_proto().cmd == []
        assert extended.to_proto().cmd == ["--help"]


# ---------------------------------------------------------------------------
# Builder methods
# ---------------------------------------------------------------------------


class TestImageRunCommands:
    """Test run_commands builder."""

    def test_single_command(self) -> None:
        img = Image.base("alpine").run_commands("apk add git")
        spec = img.to_proto()
        assert len(spec.steps) == 1
        assert spec.steps[0].run_commands.commands == ["apk add git"]

    def test_multiple_commands(self) -> None:
        img = Image.base("alpine").run_commands(
            "apt-get update", "apt-get install -y git"
        )
        spec = img.to_proto()
        assert spec.steps[0].run_commands.commands == [
            "apt-get update",
            "apt-get install -y git",
        ]

    def test_chained_run_commands(self) -> None:
        img = Image.base("alpine").run_commands("cmd1").run_commands("cmd2")
        spec = img.to_proto()
        assert len(spec.steps) == 2


class TestImagePipInstall:
    """Test pip_install builder."""

    def test_single_package(self) -> None:
        img = Image.debian_slim().pip_install(["requests"])
        assert img.to_proto().steps[0].pip_install.packages == ["requests"]

    def test_versioned_packages(self) -> None:
        img = Image.debian_slim().pip_install(["requests>=2.34", "pandas==3.0.3"])
        assert img.to_proto().steps[0].pip_install.packages == [
            "requests>=2.34",
            "pandas==3.0.3",
        ]

    def test_pip_install_from_requirements(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("requests>=2.34\n# comment\npandas\n\nnumpy\n")
            f.flush()
            img = Image.debian_slim().pip_install_from_requirements(f.name)
        assert img.to_proto().steps[0].pip_install.packages == [
            "requests>=2.34",
            "pandas",
            "numpy",
        ]


class TestImageAddFile:
    """Test add_local_file and add_local_dir builders."""

    def test_add_local_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("print('hello')")
            f.flush()
            img = Image.debian_slim().add_local_file(
                f.name, "/app/main.py", strategy="copy"
            )
        step = img.to_proto().steps[0].add_file
        assert step.destination == "/app/main.py"
        assert step.content == b"print('hello')"
        assert not step.HasField("mode")

    def test_add_local_file_with_mode(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/bash\necho hi")
            f.flush()
            img = Image.debian_slim().add_local_file(
                f.name, "/app/run.sh", mode=0o755, strategy="copy"
            )
        step = img.to_proto().steps[0].add_file
        assert step.mode == 0o755

    def test_add_local_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.py").write_text("a")
            sub = Path(d) / "sub"
            sub.mkdir()
            (sub / "b.py").write_text("b")
            img = Image.debian_slim().add_local_dir(d, "/app", strategy="copy")
        spec = img.to_proto()
        dests = [s.add_file.destination for s in spec.steps]
        assert "/app/a.py" in dests
        assert "/app/sub/b.py" in dests


# ---------------------------------------------------------------------------
# add_local_file eager validation
# ---------------------------------------------------------------------------


class TestAddLocalFileEagerValidation:
    """A3/A5: add_local_file(strategy='volume') validates dest and src at construction."""

    def test_root_dest_raises_immediately(self) -> None:
        """A3: /foo.py (parent '/') should raise at construction, not at run()."""
        with pytest.raises(ValueError, match="root"):
            Image.debian_slim().add_local_file(__file__, "/foo.py", strategy="volume")

    def test_nested_dest_accepted(self) -> None:
        """A3: /app/foo.py is valid and should not raise."""
        img = Image.debian_slim().add_local_file(
            __file__, "/app/foo.py", strategy="volume"
        )
        assert any(lf.dest == "/app/foo.py" for lf in img.lazy_local_files)

    def test_nonexistent_src_raises_immediately(self) -> None:
        """A5: a missing source file should raise FileNotFoundError at construction."""
        with pytest.raises(FileNotFoundError, match="not found"):
            Image.debian_slim().add_local_file(
                "/no/such/file.py", "/app/file.py", strategy="volume"
            )

    def test_copy_strategy_unaffected(self) -> None:
        """strategy='copy' already raised FileNotFoundError; behaviour must not change."""
        with pytest.raises((FileNotFoundError, OSError)):
            Image.debian_slim().add_local_file(
                "/no/such/file.py", "/app/file.py", strategy="copy"
            )


# ---------------------------------------------------------------------------
# add_local_dir exclude patterns
# ---------------------------------------------------------------------------


class TestAddLocalDirExclude:
    """Test that add_local_dir exclude patterns work correctly, including ** globs."""

    @staticmethod
    def _make_tree(root: str, paths: list[str]) -> None:
        """Create files at the given relative paths under *root*."""
        for p in paths:
            full = Path(root) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(p)

    def _collect_dests(self, src: str, exclude: list[str]) -> set[str]:
        """Run add_local_dir(strategy='copy') and return the set of destinations."""
        img = Image.debian_slim().add_local_dir(
            src, "/app", strategy="copy", exclude=exclude
        )
        return {s.add_file.destination for s in img.to_proto().steps}

    def test_venv_deeply_nested_excluded(self) -> None:
        """'.venv/**' must exclude files at arbitrary depth under .venv/."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    ".venv/pyvenv.cfg",
                    ".venv/lib/python3.14/site-packages/rich/pretty.py",
                    ".venv/lib/python3.14/site-packages/requests/__init__.py",
                ],
            )
            dests = self._collect_dests(d, [".venv/**"])
        assert dests == {"/app/main.py"}

    def test_named_dot_venv_deeply_nested_excluded(self) -> None:
        """'.*_venv/**' must exclude local SDK dev virtualenv directories."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    ".rene_venv/pyvenv.cfg",
                    ".rene_venv/lib/python3.11/site-packages/pyarrow/__init__.py",
                ],
            )
            dests = self._collect_dests(d, [".*_venv/**"])
        assert dests == {"/app/main.py"}

    def test_dunder_pycache_anywhere(self) -> None:
        """'**/__pycache__/**' must exclude __pycache__ at any depth."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    "__pycache__/app.cpython-314.pyc",
                    "pkg/__pycache__/mod.cpython-314.pyc",
                    "pkg/sub/__pycache__/deep.cpython-314.pyc",
                ],
            )
            dests = self._collect_dests(d, ["**/__pycache__/**"])
        assert dests == {"/app/app.py"}

    def test_star_pyc_anywhere(self) -> None:
        """'**/*.pyc' must exclude .pyc files at any depth."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    "root.pyc",
                    "pkg/mod.pyc",
                    "pkg/sub/deep.pyc",
                ],
            )
            dests = self._collect_dests(d, ["**/*.pyc"])
        assert dests == {"/app/app.py"}

    def test_prefix_wildcard_dir(self) -> None:
        """'bazel-*/**' must exclude contents of any bazel-* directory."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "BUILD",
                    "bazel-bin/pkg/lib.so",
                    "bazel-out/k8/bin/main",
                ],
            )
            dests = self._collect_dests(d, ["bazel-*/**"])
        assert dests == {"/app/BUILD"}

    def test_multiple_excludes_combined(self) -> None:
        """Multiple patterns work together (mirrors DEFAULT_EXCLUDES subset)."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "src/main.py",
                    "src/util.py",
                    ".venv/lib/python3.14/site-packages/rich/pretty.py",
                    ".git/objects/abc123",
                    ".git/HEAD",
                    "src/__pycache__/main.cpython-314.pyc",
                    "bazel-bin/pkg/lib.so",
                    ".cache/something",
                ],
            )
            dests = self._collect_dests(
                d,
                [
                    ".cache/**",
                    ".git/**",
                    "**/__pycache__/**",
                    "**/*.pyc",
                    "bazel-*/**",
                    ".venv/**",
                ],
            )
        assert dests == {"/app/src/main.py", "/app/src/util.py"}

    def test_no_excludes_includes_everything(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "a.py",
                    ".venv/lib/foo.py",
                    "__pycache__/a.pyc",
                ],
            )
            dests = self._collect_dests(d, [])
        assert len(dests) == 3

    def test_exclude_with_volume_strategy(self) -> None:
        """Exclude patterns also apply with strategy='volume'."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    ".venv/lib/python3.14/site-packages/pkg/mod.py",
                ],
            )
            img = Image.debian_slim().add_local_dir(
                d, "/app", strategy="volume", exclude=[".venv/**"]
            )
        lazy_dests = {lf.dest for lf in img.lazy_local_files}
        assert len(lazy_dests) == 1
        assert any("app.py" in d for d in lazy_dests)

    def test_star_does_not_cross_directories(self) -> None:
        """'*.pyc' only matches root-level .pyc, not nested ones."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    "root.pyc",
                    "pkg/nested.pyc",
                    "pkg/sub/deep.pyc",
                ],
            )
            dests = self._collect_dests(d, ["*.pyc"])
        # Only root.pyc excluded; nested .pyc files are kept
        assert dests == {"/app/app.py", "/app/pkg/nested.pyc", "/app/pkg/sub/deep.pyc"}

    def test_doublestar_crosses_directories(self) -> None:
        """'**/*.pyc' matches .pyc at any depth including root."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    "root.pyc",
                    "pkg/nested.pyc",
                    "pkg/sub/deep.pyc",
                ],
            )
            dests = self._collect_dests(d, ["**/*.pyc"])
        assert dests == {"/app/app.py"}

    def test_venv_only_matches_root_venv(self) -> None:
        """'.venv/**' excludes root .venv only, not a nested one."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    ".venv/lib/foo.py",
                    "projects/other/.venv/lib/bar.py",
                ],
            )
            dests = self._collect_dests(d, [".venv/**"])
        # Root .venv excluded, nested .venv kept
        assert dests == {"/app/app.py", "/app/projects/other/.venv/lib/bar.py"}


# ---------------------------------------------------------------------------
# .chalkignore / .gitignore support
# ---------------------------------------------------------------------------


class TestChalkignore:
    """Test .chalkignore and .gitignore support in add_local_dir."""

    @staticmethod
    def _make_tree(root: str, paths: list[str]) -> None:
        for p in paths:
            full = Path(root) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(p)

    @staticmethod
    def _write_ignore(root: str, filename: str, content: str) -> None:
        (Path(root) / filename).write_text(content)

    def _collect_dests(self, src: str, exclude: list[str] | None = None) -> set[str]:
        img = Image.debian_slim().add_local_dir(
            src, "/app", strategy="copy", exclude=exclude
        )
        return {s.add_file.destination for s in img.to_proto().steps}

    def test_git_dir_always_excluded(self) -> None:
        """.git/** is excluded even without any ignore file."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    ".git/HEAD",
                    ".git/objects/abc123",
                ],
            )
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py"}

    def test_chalkignore_excludes_files(self) -> None:
        """Patterns in .chalkignore are respected."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "data/big.csv",
                    "data/small.csv",
                    "src/app.py",
                ],
            )
            self._write_ignore(d, ".chalkignore", "data/**\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py", "/app/src/app.py"}

    def test_gitignore_excludes_files(self) -> None:
        """Patterns in .gitignore are also respected."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "__pycache__/main.cpython-314.pyc",
                    ".venv/lib/foo.py",
                ],
            )
            self._write_ignore(d, ".gitignore", "**/__pycache__/**\n.venv/**\n")
            dests = self._collect_dests(d)
        # .gitignore itself is a regular file and is included.
        assert dests == {"/app/main.py", "/app/.gitignore"}

    def test_chalkignore_negation_overrides_gitignore(self) -> None:
        """A ! pattern in .chalkignore can un-ignore a .gitignore match."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "logs/debug.log",
                    "logs/important.log",
                ],
            )
            self._write_ignore(d, ".gitignore", "logs/**\n")
            self._write_ignore(d, ".chalkignore", "!logs/important.log\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py", "/app/logs/important.log", "/app/.gitignore"}

    def test_chalkignore_comments_and_blanks(self) -> None:
        """Comments and blank lines in .chalkignore are ignored."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "tmp/scratch.txt",
                ],
            )
            self._write_ignore(
                d,
                ".chalkignore",
                "# This is a comment\n\ntmp/**\n\n# Another comment\n",
            )
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py"}

    def test_chalkignore_not_uploaded(self) -> None:
        """.chalkignore file itself is excluded from the upload."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, ["main.py"])
            self._write_ignore(d, ".chalkignore", "# empty\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py"}
        assert "/app/.chalkignore" not in dests

    def test_no_ignore_files_no_implicit_excludes(self) -> None:
        """Without ignore files, only .git/** is excluded (no other implicit excludes)."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    ".venv/lib/foo.py",
                    "__pycache__/a.pyc",
                ],
            )
            dests = self._collect_dests(d)
        assert len(dests) == 3

    def test_combined_with_exclude_param(self) -> None:
        """Programmatic exclude works alongside ignore files."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "data/big.csv",
                    "tmp/scratch.txt",
                ],
            )
            self._write_ignore(d, ".chalkignore", "tmp/**\n")
            dests = self._collect_dests(d, exclude=["data/**"])
        assert dests == {"/app/main.py"}

    def test_chalkignore_with_volume_strategy(self) -> None:
        """Ignore files also apply with strategy='volume'."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "app.py",
                    "data/big.csv",
                ],
            )
            self._write_ignore(d, ".chalkignore", "data/**\n")
            img = Image.debian_slim().add_local_dir(d, "/app", strategy="volume")
        lazy_dests = {lf.dest for lf in img.lazy_local_files}
        assert len(lazy_dests) == 1
        assert any("app.py" in d for d in lazy_dests)

    def test_chalkignore_escaped_hash(self) -> None:
        """A line starting with \\# matches a literal # filename."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "#notes.txt",
                ],
            )
            self._write_ignore(d, ".chalkignore", "\\#notes.txt\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py"}

    def test_gitignore_and_chalkignore_combined(self) -> None:
        """Both .gitignore and .chalkignore patterns are combined."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "__pycache__/mod.pyc",
                    "data/big.csv",
                ],
            )
            self._write_ignore(d, ".gitignore", "**/__pycache__/**\n")
            self._write_ignore(d, ".chalkignore", "data/**\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py", "/app/.gitignore"}

    def test_nested_gitignore(self) -> None:
        """A .gitignore in a subdirectory applies only within that subtree."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "root.log",
                    "src/app.py",
                    "src/debug.log",
                    "src/lib/util.py",
                    "src/lib/trace.log",
                ],
            )
            # Root .gitignore does NOT ignore .log files.
            # src/.gitignore ignores *.log only under src/ (root level only, not **).
            self._write_ignore(d, "src/.gitignore", "*.log\n")
            dests = self._collect_dests(d)
        # src/debug.log excluded (matches *.log in src domain).
        # src/lib/trace.log kept (*.log doesn't cross directories).
        # root.log kept (outside src domain).
        # src/.gitignore is a regular file, included.
        assert dests == {
            "/app/main.py",
            "/app/root.log",
            "/app/src/app.py",
            "/app/src/.gitignore",
            "/app/src/lib/util.py",
            "/app/src/lib/trace.log",
        }

    def test_nested_gitignore_doublestar(self) -> None:
        """A nested .gitignore with ** crosses directories within its subtree."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "root.log",
                    "src/app.py",
                    "src/debug.log",
                    "src/lib/trace.log",
                ],
            )
            self._write_ignore(d, "src/.gitignore", "**/*.log\n")
            dests = self._collect_dests(d)
        # root.log is NOT excluded (outside src/ domain).
        # src/debug.log and src/lib/trace.log ARE excluded.
        # src/.gitignore is a regular file, included.
        assert dests == {
            "/app/main.py",
            "/app/root.log",
            "/app/src/app.py",
            "/app/src/.gitignore",
        }

    def test_nested_chalkignore(self) -> None:
        """A .chalkignore in a subdirectory also works with domain scoping."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "data/raw/big.csv",
                    "data/raw/small.csv",
                    "data/processed/clean.csv",
                ],
            )
            self._write_ignore(d, "data/.chalkignore", "raw/**\n")
            dests = self._collect_dests(d)
        assert dests == {"/app/main.py", "/app/data/processed/clean.csv"}

    def test_nested_negation_overrides_parent(self) -> None:
        """A nested .chalkignore can negate a root .gitignore pattern."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "build/output.o",
                    "build/important/keep.o",
                ],
            )
            self._write_ignore(d, ".gitignore", "build/**\n")
            self._write_ignore(d, "build/important/.chalkignore", "!*.o\n")
            dests = self._collect_dests(d)
        assert "/app/main.py" in dests
        assert "/app/build/important/keep.o" in dests
        assert "/app/build/output.o" not in dests


# ---------------------------------------------------------------------------
# Volume grouping
# ---------------------------------------------------------------------------


class TestVolumeGrouping:
    """Test that lazy files are grouped into the right number of volumes."""

    @staticmethod
    def _make_tree(root: str, paths: list[str]) -> None:
        for p in paths:
            full = Path(root) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(p)

    def test_add_local_dir_single_volume(self) -> None:
        """All files from one add_local_dir call should produce one volume group."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(
                d,
                [
                    "main.py",
                    "pkg/__init__.py",
                    "pkg/sub/deep.py",
                    "lib/utils.py",
                ],
            )
            img = Image.debian_slim().add_local_dir(d, "/workspace", strategy="volume")
        groups = img.lazy_local_files_grouped_for_volume_mounts()
        assert list(groups.keys()) == ["/workspace"]
        assert len(groups["/workspace"]) == 4

    def test_add_local_file_groups_by_parent(self) -> None:
        """add_local_file (no mount_root) should group by parent directory."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"a")
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as g:
            g.write(b"b")
        img = (
            Image.debian_slim()
            .add_local_file(f.name, "/app/a.py", strategy="volume")
            .add_local_file(g.name, "/data/b.py", strategy="volume")
        )
        groups = img.lazy_local_files_grouped_for_volume_mounts()
        assert sorted(groups.keys()) == ["/app", "/data"]

    def test_add_local_dir_and_file_same_root_merge(self) -> None:
        """add_local_dir and add_local_file targeting the same parent merge."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, ["pkg/mod.py"])
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
                f.write(b"extra")
            img = (
                Image.debian_slim()
                .add_local_dir(d, "/app", strategy="volume")
                .add_local_file(f.name, "/app/extra.py", strategy="volume")
            )
        groups = img.lazy_local_files_grouped_for_volume_mounts()
        assert list(groups.keys()) == ["/app"]
        assert len(groups["/app"]) == 2

    def test_two_add_local_dir_different_dests(self) -> None:
        """Two add_local_dir calls with different dests produce two groups."""
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            self._make_tree(d1, ["a.py", "sub/b.py"])
            self._make_tree(d2, ["c.py"])
            img = (
                Image.debian_slim()
                .add_local_dir(d1, "/repo1", strategy="volume")
                .add_local_dir(d2, "/repo2", strategy="volume")
            )
        groups = img.lazy_local_files_grouped_for_volume_mounts()
        assert sorted(groups.keys()) == ["/repo1", "/repo2"]
        assert len(groups["/repo1"]) == 2
        assert len(groups["/repo2"]) == 1


# ---------------------------------------------------------------------------
# Other builders and serialization
# ---------------------------------------------------------------------------


class TestImageDockerfileCommands:
    """Test dockerfile_commands builder."""

    def test_raw_commands(self) -> None:
        img = Image.base("alpine").dockerfile_commands(
            ["EXPOSE 8080", "LABEL maintainer=test"]
        )
        step = img.to_proto().steps[0].dockerfile_commands
        assert step.commands == ["EXPOSE 8080", "LABEL maintainer=test"]


class TestImageEnvWorkdirEntrypointCmd:
    """Test env, workdir, entrypoint, and cmd builders."""

    def test_env_merges(self) -> None:
        img = Image.base("alpine").env({"A": "1"}).env({"B": "2", "A": "overridden"})
        env = dict(img.to_proto().env)
        assert env == {"A": "overridden", "B": "2"}

    def test_workdir(self) -> None:
        img = Image.base("alpine").workdir("/workspace")
        assert img.to_proto().workdir == "/workspace"

    def test_entrypoint(self) -> None:
        img = Image.base("alpine").entrypoint(["/bin/sh", "-c"])
        assert list(img.to_proto().entrypoint) == ["/bin/sh", "-c"]

    def test_cmd(self) -> None:
        img = Image.base("alpine").cmd(["--verbose", "--debug"])
        assert list(img.to_proto().cmd) == ["--verbose", "--debug"]


class TestImageRepr:
    """Test __repr__ for debugging."""

    def test_repr_minimal(self) -> None:
        r = repr(Image.base("alpine"))
        assert "alpine" in r

    def test_repr_with_steps(self) -> None:
        img = (
            Image.debian_slim()
            .pip_install(["requests"])
            .workdir("/app")
            .env({"K": "V"})
        )
        r = repr(img)
        assert "steps=1" in r
        assert "/app" in r
        assert "K" in r


class TestImageProtoSerialization:
    """Test proto serialization with all fields."""

    def test_full_image_serialization(self) -> None:
        img = (
            Image.debian_slim()
            .pip_install(["requests"])
            .run_commands("echo 'hello'")
            .workdir("/home/user")
            .env({"TEST_VAR": "hello"})
        )
        spec = img.to_proto()
        assert spec.base_image == "python:3.14-slim-trixie"
        assert len(spec.steps) == 2
        assert spec.steps[0].pip_install.packages == ["requests"]
        assert spec.steps[1].run_commands.commands == ["echo 'hello'"]
        assert spec.workdir == "/home/user"
        assert spec.env["TEST_VAR"] == "hello"

    def test_json_serialization_for_connect(self) -> None:
        from google.protobuf.json_format import MessageToDict

        img = Image.debian_slim().pip_install(["requests"]).env({"K": "V"})
        spec_dict = MessageToDict(img.to_proto(), preserving_proto_field_name=True)
        assert spec_dict["base_image"] == "python:3.14-slim-trixie"
        assert "steps" in spec_dict
        assert "env" in spec_dict

    def test_empty_image_serialization(self) -> None:
        """An image with no steps should serialize cleanly."""
        from google.protobuf.json_format import MessageToDict

        img = Image.base("scratch")
        spec = img.to_proto()
        assert spec.base_image == "scratch"
        assert len(spec.steps) == 0
        spec_dict = MessageToDict(spec, preserving_proto_field_name=True)
        assert spec_dict == {"base_image": "scratch"}


class TestImageChaining:
    """Test complex chaining patterns."""

    def test_shared_base_diverges(self) -> None:
        """Two images can share a base and diverge without interference."""
        base = Image.debian_slim().pip_install(["requests"])
        branch_a = base.pip_install(["pandas"])
        branch_b = base.run_commands("apt-get update")
        assert len(base.to_proto().steps) == 1
        assert len(branch_a.to_proto().steps) == 2
        assert len(branch_b.to_proto().steps) == 2
        assert branch_a.to_proto().steps[1].pip_install.packages == ["pandas"]
        assert branch_b.to_proto().steps[1].run_commands.commands == ["apt-get update"]

    def test_long_chain(self) -> None:
        img = (
            Image.debian_slim()
            .run_commands("apt-get update")
            .pip_install(["requests", "pandas"])
            .add_local_file(__file__, "/app/test.py", strategy="copy")
            .dockerfile_commands(["EXPOSE 8080"])
            .env({"APP": "test"})
            .workdir("/app")
            .entrypoint(["python"])
            .cmd(["test.py"])
        )
        spec = img.to_proto()
        assert len(spec.steps) == 4
        assert spec.base_image == "python:3.14-slim-trixie"
        assert spec.workdir == "/app"
        assert spec.env["APP"] == "test"
        assert list(spec.entrypoint) == ["python"]
        assert list(spec.cmd) == ["test.py"]
