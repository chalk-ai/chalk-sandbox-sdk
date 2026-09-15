"""Developer-only helpers for local deploy overlays."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

_LOCAL_COPY_ENV_VAR = "CHALK_FUNCTION_LOCAL_SDK_COPY"
_LOCAL_WHEEL_ENV_VAR = "CHALK_FUNCTION_LOCAL_SDK_WHEEL"
_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR = "CHALK_FUNCTION_REMOTE_CALL_WHEEL"
_STRIP_SOURCE_ENV_VAR = "CHALK_FUNCTION_STRIP_SOURCE"

_LOCAL_SDK_MOUNT = "/tmp/sdk"
_LOCAL_REMOTE_CALL_MOUNT = "/tmp/remote-call-wheel"
_LOCAL_SDK_COPY_EXCLUDES = [
    "tests/**",
    "e2etests/**",
    "examples/**",
    ".venv/**",
    ".*_venv/**",
    "venv/**",
    "**/__pycache__/**",
    "**/target/**",
]
_LOCAL_SDK_RUNTIME_PACKAGES = [
    "pyyaml>=6.0.0",
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-otlp-proto-http>=1.20.0",
    "opentelemetry-exporter-otlp-proto-grpc>=1.20.0",
    "rich>=13.0.0",
    "protobuf-py==0.3.0",
    # ConnectRPC 0.11.1 pins protobuf-py 0.1.1; upstream main supports 0.3.0.
    "connectrpc @ https://github.com/connectrpc/connect-py/archive/c19b11325be765855f4a4fae209251d3aa7f5d57.tar.gz",
    "pyqwest>=0.9.0",
]


def running_in_function_pod() -> bool:
    return bool(os.environ.get("CHALK_REMOTE_FUNCTION"))


def build_function_runtime_image(*, python_version: str = "3.11") -> object:
    """Return the standard deploy image used for local function SDK testing."""
    from chalkcompute._image import Image

    return Image.debian_slim(python_version).pip_install(["chalkcompute==0.0.0"])


def function_runtime_image_or_none(*, python_version: str = "3.11") -> object | None:
    """Return the standard local-test runtime image unless already in the pod."""
    if running_in_function_pod():
        return None
    return build_function_runtime_image(python_version=python_version)


def should_strip_source() -> bool:
    if _STRIP_SOURCE_ENV_VAR in os.environ:
        return os.environ[_STRIP_SOURCE_ENV_VAR] == "1"

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("chalkcompute") == "0.0.0"
    except PackageNotFoundError:
        return False


@dataclass(frozen=True)
class LocalDeployConfig:
    strip_source: bool
    sdk_copy_enabled: bool
    sdk_wheel_path: str | None
    remote_call_wheel_path: str | None

    @property
    def should_mount_sdk_source(self) -> bool:
        return (
            self.sdk_copy_enabled
            and self.sdk_wheel_path is None
            and not self.strip_source
        )

    @property
    def should_install_sdk_wheel(self) -> bool:
        return self.sdk_wheel_path is not None and not self.strip_source


def resolve_local_deploy_config() -> LocalDeployConfig:
    strip_source = should_strip_source()
    sdk_wheel_path = os.environ.get(_LOCAL_WHEEL_ENV_VAR, "") or None
    remote_call_wheel_path = (
        os.environ.get(_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR, "") or None
    )
    sdk_copy_enabled = os.environ.get(_LOCAL_COPY_ENV_VAR, "") == "1"
    return LocalDeployConfig(
        strip_source=strip_source,
        sdk_copy_enabled=sdk_copy_enabled,
        sdk_wheel_path=sdk_wheel_path,
        remote_call_wheel_path=remote_call_wheel_path,
    )


def wrap_entrypoint_for_local_dev(handler_cmd: list[str]) -> list[str]:
    config = resolve_local_deploy_config()
    command = " ".join(shlex.quote(part) for part in handler_cmd)
    prelude: list[str] = []

    if config.remote_call_wheel_path:
        wheel_name = shlex.quote(Path(config.remote_call_wheel_path).name)
        prelude.append(
            "python -m pip install --force-reinstall "
            f"{_LOCAL_REMOTE_CALL_MOUNT}/{wheel_name}"
        )

    if config.should_install_sdk_wheel:
        assert config.sdk_wheel_path is not None
        wheel_name = shlex.quote(Path(config.sdk_wheel_path).name)
        prelude.append(
            "python -m pip install --force-reinstall --no-deps "
            f"{_LOCAL_SDK_MOUNT}/{wheel_name}"
        )
    elif config.should_mount_sdk_source:
        from chalkcompute._image import _ARCHIVE_CHUNK_PREFIX, ARCHIVE_NAME

        extract_dir = "/tmp/chalkcompute-sdk-src"
        archive_path = f"/tmp/{ARCHIVE_NAME}"
        prelude.extend(
            [
                f"cat {_LOCAL_SDK_MOUNT}/{_ARCHIVE_CHUNK_PREFIX}* > {archive_path}",
                f"rm -rf {extract_dir}; mkdir -p {extract_dir}; tar xzf {archive_path} -C {extract_dir}",
                f"export PYTHONPATH={extract_dir}:$PYTHONPATH",
            ]
        )

    if not prelude:
        return handler_cmd
    return ["sh", "-c", f"set -eux; {'; '.join(prelude)}; exec {command}"]


def apply_local_dev_mounts(
    base_image: object,
    *,
    remote_call_runtime_package: str,
) -> object:
    from chalkcompute._image import Image

    assert isinstance(base_image, Image)
    config = resolve_local_deploy_config()
    image = base_image

    if config.should_install_sdk_wheel:
        assert config.sdk_wheel_path is not None
        path = Path(config.sdk_wheel_path)
        image = image.pip_install(
            [remote_call_runtime_package, *_LOCAL_SDK_RUNTIME_PACKAGES]
        ).add_local_file(str(path), f"{_LOCAL_SDK_MOUNT}/{path.name}")
    elif config.should_mount_sdk_source:
        project_root = Path(__file__).resolve().parent.parent
        image = image.pip_install(
            [remote_call_runtime_package, *_LOCAL_SDK_RUNTIME_PACKAGES]
        ).add_local_dir(
            str(project_root),
            _LOCAL_SDK_MOUNT,
            exclude=_LOCAL_SDK_COPY_EXCLUDES,
            archive=True,
        )
    elif config.strip_source:
        image = image.pip_install([remote_call_runtime_package])
    else:
        from importlib.metadata import version as _pkg_version

        image = image.pip_install(
            [
                remote_call_runtime_package,
                f"chalkcompute=={_pkg_version('chalkcompute')}",
            ]
        )

    if config.remote_call_wheel_path:
        path = Path(config.remote_call_wheel_path)
        image = image.add_local_file(
            str(path),
            f"{_LOCAL_REMOTE_CALL_MOUNT}/{path.name}",
            strategy="volume",
        )

    return image
