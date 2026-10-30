#!/usr/bin/env python3
"""Check one exact release wheel and install it as an unprivileged customer would."""

import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile


def select_wheel(dist: Path, python_version: str, platform_tag: str) -> Path:
    """Require exactly one artifact for this Python ABI and OS/CPU/libc target."""
    python_tag = "cp" + python_version.replace(".", "")
    matches = []
    for wheel in sorted(dist.glob("*.whl")):
        parts = wheel.stem.split("-")
        if len(parts) not in (5, 6) or parts[0] != "chalkcompute":
            continue
        interpreter, abi, platforms = parts[-3:]
        if (
            interpreter == python_tag
            and abi == python_tag
            and platform_tag in platforms.split(".")
        ):
            matches.append(wheel)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {python_tag}/{platform_tag} chalkcompute wheel; "
            f"found {len(matches)}: {[wheel.name for wheel in matches]}"
        )
    return matches[0]


def verify_contents(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    forbidden = []
    for name in names:
        parts = PurePosixPath(name).parts
        if any(
            part.startswith((".env", "test_"))
            or part in {"tests", "e2etests", ".sops.yaml"}
            or part.endswith(".enc")
            for part in parts
        ):
            forbidden.append(name)
    if forbidden:
        raise ValueError(f"Sensitive or test files found in {wheel.name}: {forbidden}")
    if not any(
        name.startswith("chalkcompute/_native.") and name.endswith((".so", ".pyd"))
        for name in names
    ):
        raise ValueError(f"No chalkcompute native extension found in {wheel.name}")


def smoke_install(wheel: Path, *, sdk_only: bool = False) -> None:
    if os.environ.get("CHALK_PRIVATE_READ_TOKEN"):
        raise RuntimeError(
            "Private repository credentials must not be available during verification"
        )

    # A separate job supplies no build credentials. Also prevent local configuration,
    # source-tree imports, and cached packages from disguising a broken public install.
    with tempfile.TemporaryDirectory(prefix="chalkcompute-wheel-") as tmp:
        work = Path(tmp)
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith(("PIP_", "UV_", "PYTHON", "GIT_", "CARGO_")):
                environment.pop(key)
        environment.update(
            HOME=str(work),
            USERPROFILE=str(work),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            PIP_CONFIG_FILE=os.devnull,
        )
        venv = work / "venv"
        subprocess.run(
            [sys.executable, "-I", "-m", "venv", str(venv)],
            check=True,
            cwd=work,
            env=environment,
        )
        python = venv / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--index-url",
                "https://pypi.org/simple",
                str(wheel.resolve()),
            ]
            + ([] if sdk_only else ["chalkpy[runtime]"]),
            check=True,
            cwd=work,
            env=environment,
        )
        subprocess.run(
            [str(python), "-I", "-m", "pip", "check"],
            check=True,
            cwd=work,
            env=environment,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.metadata import version; "
                + (
                    ""
                    if sdk_only
                    else "import chalk; print('chalkpy', version('chalkpy')); "
                )
                + "import chalkcompute; import chalkcompute._native as native; "
                "print('chalkcompute', version('chalkcompute')); "
                "print('native clients', native.RemoteCallClient, native.VolumeNativeClient); "
                "assert native.notebook_source_dependencies(['x = 1', 'y = x + 1']) == [0, 1]",
            ],
            check=True,
            cwd=work,
            env=environment,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--platform-tag", required=True)
    parser.add_argument("--contents-only", action="store_true")
    parser.add_argument(
        "--sdk-only",
        action="store_true",
        help="Verify just chalkcompute (musl only; chalkpy runtime has no complete musl wheels)",
    )
    args = parser.parse_args()
    wheel = select_wheel(args.dist, args.python_version, args.platform_tag)
    print(f"Verifying {wheel}", flush=True)
    verify_contents(wheel)
    if not args.contents_only:
        actual_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        if actual_version != args.python_version:
            raise RuntimeError(
                f"Expected Python {args.python_version}, running {actual_version}"
            )
        smoke_install(wheel, sdk_only=args.sdk_only)


if __name__ == "__main__":
    main()
