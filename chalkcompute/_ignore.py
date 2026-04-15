"""Ignore-file parsing for .gitignore and .chalkignore support.

Recursively walks the source tree and loads ``.gitignore`` and ``.chalkignore``
files from every directory, attaching a domain (directory prefix) so patterns
apply only within their subtree — matching the behavior of go-git's
``ReadPatterns``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IgnorePattern:
    """A single ignore pattern, possibly negated, scoped to a directory.

    *domain* is the relative path (using ``/`` separators) from the source root
    to the directory containing the ignore file.  An empty string means the
    pattern was defined at the root.  When matching, only paths that start with
    *domain* are considered, and the pattern is applied to the path portion
    **after** the domain prefix.
    """

    pattern: str
    negation: bool = False
    domain: str = ""


_ALWAYS_EXCLUDED: list[IgnorePattern] = [
    IgnorePattern(pattern=".git/**"),
]

_IGNORE_FILENAMES = (".gitignore", ".chalkignore")


def _parse_ignore_lines(text: str) -> list[tuple[str, bool]]:
    """Parse ignore file text into ``(pattern, negation)`` pairs.

    Handles ``#`` comments, blank lines, ``!`` negation, trailing whitespace,
    and ``\\#`` escapes for literal hash characters.
    """
    results: list[tuple[str, bool]] = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("\\#"):
            stripped = stripped[1:]
        negation = False
        if stripped.startswith("!"):
            negation = True
            stripped = stripped[1:]
        if not stripped:
            continue
        results.append((stripped, negation))
    return results


def collect_ignore_patterns(src_dir: Path) -> list[IgnorePattern]:
    """Collect ignore patterns by walking *src_dir* recursively.

    For every directory (starting at the root), any ``.gitignore`` and
    ``.chalkignore`` files are read and their patterns are recorded with the
    directory's relative path as the domain.

    Order within each directory: ``.gitignore`` first, then ``.chalkignore``.
    Directories are visited in sorted order (parents before children) so that
    negation in deeper directories can override shallower rules.

    The ``.git/`` directory is always excluded via a hardcoded pattern.
    """
    patterns: list[IgnorePattern] = list(_ALWAYS_EXCLUDED)

    # Collect all directories (including root) sorted so parents come first.
    dirs: list[Path] = [src_dir]
    for child in sorted(src_dir.rglob("*")):
        if not child.is_dir():
            continue
        # Skip .git directories entirely.
        try:
            rel = child.relative_to(src_dir)
        except ValueError:
            continue
        rel_str = str(rel)
        if rel_str == ".git" or rel_str.startswith(".git/"):
            continue
        dirs.append(child)

    for d in dirs:
        if d == src_dir:
            domain = ""
        else:
            domain = str(d.relative_to(src_dir))

        for filename in _IGNORE_FILENAMES:
            ignore_file = d / filename
            if not ignore_file.is_file():
                continue
            for pat, negation in _parse_ignore_lines(ignore_file.read_text()):
                patterns.append(
                    IgnorePattern(pattern=pat, negation=negation, domain=domain)
                )

    return patterns


def is_excluded(rel_path: str, patterns: list[IgnorePattern]) -> bool:
    """Check if a relative path is excluded by the given patterns.

    Evaluates patterns in order; the last matching rule wins.  Each pattern's
    *domain* is checked first — a pattern with ``domain="src/pkg"`` only
    considers paths under ``src/pkg/`` and matches against the remainder.
    """
    result = False
    for p in patterns:
        # Determine the path portion to match against.
        if p.domain:
            # Pattern only applies within its domain subtree.
            prefix = p.domain + "/"
            if not rel_path.startswith(prefix):
                continue
            match_path = rel_path[len(prefix):]
        else:
            match_path = rel_path
        if _match_exclude(match_path, p.pattern):
            result = not p.negation
    return result


# ---------------------------------------------------------------------------
# Pattern matching (gitignore-style)
# ---------------------------------------------------------------------------


def _match_exclude(rel_str: str, pattern: str) -> bool:
    """Match a relative path against an exclude pattern (gitignore-style).

    ``*`` matches within a single path component, ``**`` matches zero or more
    complete components.  Patterns are anchored to the source directory root.
    """
    return _match_segments(rel_str.split("/"), pattern.split("/"))


def _match_segments(path_segs: list[str], pat_segs: list[str]) -> bool:
    if not pat_segs:
        return not path_segs
    if pat_segs[0] == "**":
        rest = pat_segs[1:]
        for skip in range(len(path_segs) + 1):
            if _match_segments(path_segs[skip:], rest):
                return True
        return False
    if not path_segs:
        return False
    if fnmatch.fnmatchcase(path_segs[0], pat_segs[0]):
        return _match_segments(path_segs[1:], pat_segs[1:])
    return False
