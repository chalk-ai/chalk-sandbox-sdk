"""
Transitive local dependency collection for ``@chalkcompute.function``.

Given the source file of a user function, walk the import graph (via AST) and
collect every other Python source file in the *same project* that is reachable
through imports. Pip-installed packages and stdlib modules are excluded.

The output is a list of :class:`LocalSource` entries, each carrying the
absolute path on disk, the dotted module name, and the mount path on the image
(under ``/app``). Preserving the dotted path means relative and absolute
intra-project imports keep working inside the container.
"""

from __future__ import annotations

import ast
import logging
import site
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_MOUNT_PREFIX = "/app"


@dataclass(frozen=True)
class LocalSource:
    """A local source file to ship with the deployed function."""

    path: Path
    """Absolute path on disk."""

    module_name: str
    """Dotted module name (e.g. ``mypkg.subpkg.mod``)."""

    mount_path: str
    """Destination path on the image (under ``/app``)."""


@dataclass(frozen=True)
class CollectedSources:
    """Result of :func:`collect_local_sources`."""

    entry: LocalSource
    """The file defining the decorated function."""

    sources: list[LocalSource]
    """Entry + transitively-imported local files, deduplicated."""

    sys_path_root: Path
    """Directory that must be on ``sys.path`` to resolve these modules."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_sys_path_root(source_file: Path) -> Path:
    """Return the directory that should be on ``sys.path`` for *source_file*.

    Walks up while each parent directory contains an ``__init__.py``. For a
    file at ``/a/b/mypkg/mod.py`` where ``mypkg`` is a package but ``/a/b``
    is not, this returns ``/a/b``. For a loose script at ``/a/b/script.py``
    it returns ``/a/b``.
    """
    current = source_file.parent.resolve()
    while (current / "__init__.py").is_file():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def module_name_for(file: Path, sys_path_root: Path) -> str:
    """Return the dotted module name of *file* relative to *sys_path_root*."""
    rel = file.resolve().relative_to(sys_path_root.resolve())
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    return ".".join(parts)


def collect_local_sources(entry_file: Path) -> CollectedSources:
    """Walk imports from *entry_file* and return every reachable local file.

    Files in site-packages, user site, and the stdlib are excluded. The entry
    file itself is always included as the first result.
    """
    entry_file = entry_file.resolve()
    sys_path_root = find_sys_path_root(entry_file)

    if _is_excluded_root(sys_path_root):
        logger.debug(
            "sys.path root %s is in site-packages/stdlib; skipping dep walk",
            sys_path_root,
        )
        entry = _make_source(entry_file, sys_path_root)
        return CollectedSources(entry=entry, sources=[entry], sys_path_root=sys_path_root)

    visited: dict[Path, LocalSource] = {}
    queue: list[Path] = [entry_file]

    while queue:
        path = queue.pop()
        if path in visited:
            continue
        if not _is_under(path, sys_path_root):
            continue
        src = _make_source(path, sys_path_root)
        visited[path] = src

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as e:
            logger.debug("Skipping AST parse for %s: %s", path, e)
            continue

        for dep in _resolve_imports(tree, path, sys_path_root):
            if dep not in visited:
                queue.append(dep)

    entry_src = visited[entry_file]
    # Stable ordering: entry first, then by module name
    others = sorted(
        (s for p, s in visited.items() if p != entry_file),
        key=lambda s: s.module_name,
    )
    return CollectedSources(
        entry=entry_src,
        sources=[entry_src, *others],
        sys_path_root=sys_path_root,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _make_source(file: Path, sys_path_root: Path) -> LocalSource:
    module_name = module_name_for(file, sys_path_root)
    rel = file.resolve().relative_to(sys_path_root.resolve())
    mount = f"{_MOUNT_PREFIX}/{rel.as_posix()}"
    return LocalSource(path=file.resolve(), module_name=module_name, mount_path=mount)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _excluded_roots() -> list[Path]:
    roots: list[str] = []
    roots.extend(site.getsitepackages())
    user_site = site.getusersitepackages()
    if user_site:
        roots.append(user_site)
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            roots.append(p)
    # sys.prefix / sys.base_prefix catch framework builds on macOS.
    roots.append(sys.prefix)
    roots.append(sys.base_prefix)
    resolved: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        try:
            p = Path(r).resolve()
        except OSError:
            continue
        if p not in seen:
            seen.add(p)
            resolved.append(p)
    return resolved


def _is_excluded_root(path: Path) -> bool:
    p = path.resolve()
    for ex in _excluded_roots():
        try:
            p.relative_to(ex)
            return True
        except ValueError:
            continue
    return False


def _resolve_imports(
    tree: ast.AST,
    importing_file: Path,
    sys_path_root: Path,
) -> list[Path]:
    """Collect local file paths for every import statement in *tree*."""
    results: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.extend(_resolve_absolute(alias.name, sys_path_root))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                absolute = _relative_to_absolute(
                    importing_file=importing_file,
                    sys_path_root=sys_path_root,
                    level=node.level,
                    module=node.module,
                )
                if absolute is None:
                    continue
            else:
                absolute = node.module
            if not absolute:
                continue
            # The `from X import Y, Z` form may import submodules, names from X,
            # or both. Resolve X itself and also speculatively try each X.Y.
            results.extend(_resolve_absolute(absolute, sys_path_root))
            for alias in node.names:
                if alias.name == "*":
                    continue
                results.extend(
                    _resolve_absolute(f"{absolute}.{alias.name}", sys_path_root)
                )
    return results


def _resolve_absolute(dotted: str, sys_path_root: Path) -> list[Path]:
    """Resolve a dotted module name to local file paths.

    Returns the module file plus every ancestor package's ``__init__.py``.
    Skips site-packages / stdlib even if they shadow the project name.
    """
    parts = dotted.split(".")
    results: list[Path] = []
    # Ancestor __init__.py files
    for i in range(1, len(parts) + 1):
        base = sys_path_root.joinpath(*parts[:i])
        init = base / "__init__.py"
        if init.is_file():
            results.append(init.resolve())
    # Module file itself
    leaf = sys_path_root.joinpath(*parts).with_suffix(".py")
    if leaf.is_file():
        results.append(leaf.resolve())
    return results


def _relative_to_absolute(
    *,
    importing_file: Path,
    sys_path_root: Path,
    level: int,
    module: str | None,
) -> str | None:
    """Translate a relative import into an absolute dotted module name.

    ``from . import X`` inside a module ``pkg.sub.mod`` resolves to ``pkg.sub``
    (plus ``pkg.sub.X`` once we try submodule resolution upstream). Inside
    ``pkg.sub/__init__.py`` the anchor is ``pkg.sub`` itself.
    """
    try:
        rel = importing_file.resolve().relative_to(sys_path_root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__.py"
    if is_init:
        package_parts = parts[:-1]
    else:
        package_parts = parts[:-1]  # drop the module filename
    # Strip trailing ".py" just in case (defensive)
    # level 1 = current package; level 2 = one parent up; etc.
    drop = level - 1
    if drop > len(package_parts):
        return None
    anchor = package_parts[: len(package_parts) - drop] if drop > 0 else list(package_parts)
    if module:
        anchor = anchor + module.split(".")
    return ".".join(anchor) if anchor else None
