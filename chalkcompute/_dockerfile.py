"""Dockerfile parsing for ``Image.from_dockerfile``.

Image builds carry no build context, so a ``COPY``/``ADD`` of a local path
cannot be passed through to the builder. This module splits a Dockerfile into
instructions and resolves each local ``COPY``/``ADD`` into the concrete files
it would copy, so ``Image.from_dockerfile`` can ship them as ``add_file``
steps or lazy volume files instead.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from pathlib import Path

from chalkcompute._ignore import _match_exclude

_URL_PREFIXES = ("http://", "https://", "git@", "git://", "ssh://")
_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar.zst",
)
# --link only changes layer caching, which has no equivalent once files are
# shipped as build steps, so it is safe to drop.
_IGNORED_COPY_FLAGS = ("--link",)


@dataclass(frozen=True)
class DockerfileInstruction:
    """One logical Dockerfile instruction, with line continuations joined."""

    line: int
    """1-based line number where the instruction starts."""

    keyword: str
    """Upper-cased instruction keyword, e.g. ``RUN``."""

    text: str
    """The instruction's physical lines, stripped, joined with newlines."""

    args: str
    """Everything after the keyword, with continuations joined by spaces."""


@dataclass(frozen=True)
class ContextFile:
    """A file from the build context that a ``COPY``/``ADD`` places in the image."""

    src: Path
    """Absolute local path."""

    dest: str
    """Absolute destination path in the image."""

    mode: int
    """POSIX permission bits to apply at the destination."""

    mount_root: str | None
    """Destination directory for files copied from one source directory."""


def parse_dockerfile(content: str) -> list[DockerfileInstruction]:
    """Split Dockerfile text into logical instructions.

    Comments, blank lines, and parser directives are dropped. Comment and
    blank lines inside a continuation are skipped, as Docker does.
    """
    escape = _escape_char(content)
    instructions: list[DockerfileInstruction] = []
    physical: list[str] = []
    joined: list[str] = []
    start = 0
    for lineno, raw in enumerate(content.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not physical:
            start = lineno
        physical.append(stripped)
        if stripped.endswith(escape):
            joined.append(stripped[: -len(escape)].strip())
            continue
        joined.append(stripped)
        instructions.append(_make_instruction(start, physical, joined))
        physical, joined = [], []
    if physical:
        instructions.append(_make_instruction(start, physical, joined))
    return instructions


def _escape_char(content: str) -> str:
    """Return the ``# escape=`` parser directive, or the default backslash."""
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("#"):
            break
        key, sep, value = stripped[1:].partition("=")
        if not sep:
            break
        if key.strip().lower() == "escape":
            return value.strip()
    return "\\"


def _make_instruction(
    start: int, physical: list[str], joined: list[str]
) -> DockerfileInstruction:
    keyword, _, args = " ".join(part for part in joined if part).partition(" ")
    return DockerfileInstruction(
        line=start,
        keyword=keyword.upper(),
        text="\n".join(physical),
        args=args.strip(),
    )


def resolve_workdir(current: str | None, arg: str) -> str | None:
    """Apply a ``WORKDIR`` argument; ``None`` means the workdir is unknown."""
    if "$" in arg:
        return None
    if arg.startswith("/"):
        return posixpath.normpath(arg)
    if current is None:
        # Relative to the base image's own WORKDIR, which is not visible here.
        return None
    return posixpath.normpath(posixpath.join(current, arg))


def load_dockerignore(context: Path) -> list[tuple[str, bool]]:
    """Return ``(pattern, negation)`` pairs from ``context/.dockerignore``."""
    path = context / ".dockerignore"
    if not path.is_file():
        return []
    patterns: list[tuple[str, bool]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negation = line.startswith("!")
        if negation:
            line = line[1:].strip()
        pattern = posixpath.normpath(line.lstrip("/"))
        if pattern == ".":
            continue
        patterns.append((pattern, negation))
    return patterns


def _dockerignored(rel: str, patterns: list[tuple[str, bool]]) -> bool:
    """Apply .dockerignore rules; a pattern matching a parent excludes its tree."""
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    excluded = False
    for pattern, negation in patterns:
        if any(_match_exclude(prefix, pattern) for prefix in prefixes):
            excluded = not negation
    return excluded


def resolve_copy(
    instruction: DockerfileInstruction,
    *,
    context: Path,
    workdir: str | None,
    dockerignore: list[tuple[str, bool]],
) -> list[ContextFile] | None:
    """Resolve a ``COPY``/``ADD`` into the context files it copies.

    Returns ``None`` when the instruction reads nothing from the build context
    (``--from=`` or an ``ADD`` of URLs) and can be passed through unchanged.

    Raises
    ------
    ValueError
        If the instruction uses a form that cannot be reproduced without a
        build context.
    """
    tokens = _split_args(instruction)
    flags: list[str] = []
    while tokens and tokens[0].startswith("--"):
        flags.append(tokens.pop(0))
    if any(flag.startswith("--from=") for flag in flags):
        return None
    if len(tokens) < 2:
        raise _copy_error(instruction, "expected at least one source and a destination")
    *sources, dest = tokens

    is_url = [src.startswith(_URL_PREFIXES) for src in sources]
    if all(is_url) and instruction.keyword == "ADD":
        return None
    if any(is_url):
        raise _copy_error(
            instruction,
            "cannot mix URL and local sources; use a separate ADD for the URLs",
        )

    mode: int | None = None
    for flag in flags:
        name, _, value = flag.partition("=")
        if name == "--chmod":
            try:
                mode = int(value, 8)
            except ValueError:
                raise _copy_error(
                    instruction, f"--chmod must be an octal mode, got {value!r}"
                ) from None
        elif name not in _IGNORED_COPY_FLAGS:
            raise _copy_error(instruction, f"the {name} flag is not supported")

    for token in [*sources, dest]:
        if token.startswith("<<"):
            raise _copy_error(instruction, "heredocs are not supported")
        if "$" in token:
            raise _copy_error(
                instruction, f"variable substitution in {token!r} is not supported"
            )

    if dest.startswith("/"):
        dest_path = dest
    elif workdir is not None:
        dest_path = posixpath.join(workdir, dest)
    else:
        raise _copy_error(
            instruction,
            f"relative destination {dest!r} needs a known WORKDIR; "
            "use an absolute destination or set an absolute WORKDIR first",
        )

    context = context.resolve()
    matched: list[Path] = []
    for src in sources:
        hits = _glob_context(context, src, instruction)
        if not hits:
            raise _copy_error(
                instruction, f"no files in {str(context)!r} match {src!r}"
            )
        matched.extend(hits)

    dest_is_dir = dest.endswith("/") or dest in (".", "..")
    if len(matched) > 1 and not dest_is_dir:
        raise _copy_error(
            instruction,
            "when copying more than one source, the destination must end with '/'",
        )

    files: list[ContextFile] = []
    for src_path in matched:
        if src_path.is_dir():
            root = posixpath.normpath(dest_path)
            for file_path in sorted(src_path.rglob("*")):
                if not file_path.is_file():
                    continue
                if _dockerignored(
                    file_path.relative_to(context).as_posix(), dockerignore
                ):
                    continue
                rel = file_path.relative_to(src_path).as_posix()
                files.append(
                    ContextFile(
                        src=file_path,
                        dest=posixpath.join(root, rel),
                        mode=_file_mode(file_path, mode),
                        mount_root=root,
                    )
                )
            continue
        if _dockerignored(src_path.relative_to(context).as_posix(), dockerignore):
            raise _copy_error(
                instruction, f"source {src_path.name!r} is excluded by .dockerignore"
            )
        if instruction.keyword == "ADD" and src_path.name.endswith(_ARCHIVE_SUFFIXES):
            raise _copy_error(
                instruction,
                f"ADD of local archive {src_path.name!r} would be auto-extracted, "
                "which is not supported; extract it in a RUN step instead",
            )
        file_dest = (
            posixpath.join(dest_path, src_path.name)
            if dest_is_dir
            else posixpath.normpath(dest_path)
        )
        files.append(
            ContextFile(
                src=src_path,
                dest=posixpath.normpath(file_dest),
                mode=_file_mode(src_path, mode),
                mount_root=None,
            )
        )
    return files


def _split_args(instruction: DockerfileInstruction) -> list[str]:
    """Split COPY/ADD arguments, accepting both shell and JSON forms."""
    args = instruction.args
    flags: list[str] = []
    rest = args.split()
    while rest and rest[0].startswith("--"):
        flags.append(rest.pop(0))
    remainder = " ".join(rest)
    if remainder.startswith("["):
        try:
            parsed = json.loads(remainder)
        except json.JSONDecodeError:
            raise _copy_error(instruction, "malformed JSON argument list") from None
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise _copy_error(instruction, "JSON form must be a list of strings")
        return [*flags, *parsed]
    return [*flags, *rest]


def _glob_context(
    context: Path, src: str, instruction: DockerfileInstruction
) -> list[Path]:
    """Expand ``src`` within ``context``, rejecting paths that escape it."""
    rel = posixpath.normpath(src.lstrip("/"))
    if rel == ".":
        hits = [context]
    elif any(ch in rel for ch in "*?["):
        hits = sorted(context.glob(rel))
    else:
        candidate = context / rel
        hits = [candidate] if candidate.exists() else []
    resolved: list[Path] = []
    for hit in hits:
        path = hit.resolve()
        if path != context and context not in path.parents:
            raise _copy_error(
                instruction, f"source {src!r} is outside the build context"
            )
        resolved.append(path)
    return resolved


def _file_mode(path: Path, chmod: int | None) -> int:
    """Docker keeps source permissions unless ``--chmod`` overrides them."""
    if chmod is not None:
        return chmod
    return path.stat().st_mode & 0o7777


def _copy_error(instruction: DockerfileInstruction, reason: str) -> ValueError:
    return ValueError(
        f"from_dockerfile: cannot reproduce {instruction.keyword} on line "
        f"{instruction.line} ({instruction.text!r}): {reason}"
    )
