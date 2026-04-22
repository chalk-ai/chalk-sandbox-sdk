"""Chalkbox — SDK for Chalk sandboxes, containers, and volumes."""

import importlib

from chalkcompute._logging import setup_logging as _setup_logging

_setup_logging()

_LAZY_MAP: dict[str, tuple[str, str]] = {
    # _container.py
    "Container": ("chalkcompute._container", "Container"),
    "ContainerError": ("chalkcompute._container", "ContainerError"),
    "ContainerInfo": ("chalkcompute._container", "ContainerInfo"),
    "ContainerExecResult": ("chalkcompute._container", "ExecResult"),
    "ImageBuildError": ("chalkcompute._container", "ImageBuildError"),
    # _cls.py
    "RemoteClass": ("chalkcompute._cls", "RemoteClass"),
    "ClassError": ("chalkcompute._cls", "ClassError"),
    "ClassBuildError": ("chalkcompute._cls", "ClassBuildError"),
    "cls": ("chalkcompute._cls", "cls"),
    "method": ("chalkcompute._cls", "method"),
    "before": ("chalkcompute._cls", "before"),
    "after": ("chalkcompute._cls", "after"),
    # _function.py
    "RemoteFunction": ("chalkcompute._function", "RemoteFunction"),
    "DeployedFunction": ("chalkcompute._function", "DeployedFunction"),
    "FunctionError": ("chalkcompute._function", "FunctionError"),
    "FunctionBuildError": ("chalkcompute._function", "FunctionBuildError"),
    "FunctionVersionInfo": ("chalkcompute._function", "FunctionVersionInfo"),
    "RetryPolicy": ("chalkcompute._retry", "RetryPolicy"),
    "RateLimitPolicy": ("chalkcompute._rate_limit", "RateLimitPolicy"),
    "ConcurrencyPolicy": ("chalkcompute._concurrency", "ConcurrencyPolicy"),
    "function": ("chalkcompute._function", "function"),
    # _image.py
    "Image": ("chalkcompute._image", "Image"),
    "LazyLocalFile": ("chalkcompute._image", "LazyLocalFile"),
    # _secret.py
    "Secret": ("chalkcompute._secret", "Secret"),
    # _sandbox.py
    "ExecError": ("chalkcompute._sandbox", "ExecError"),
    "ExecEvent": ("chalkcompute._sandbox", "ExecEvent"),
    "ExecProcess": ("chalkcompute._sandbox", "ExecProcess"),
    "ExecResult": ("chalkcompute._sandbox", "ExecResult"),
    "Sandbox": ("chalkcompute._sandbox", "Sandbox"),
    "SandboxBuildError": ("chalkcompute._sandbox", "SandboxBuildError"),
    "SandboxClient": ("chalkcompute._sandbox", "SandboxClient"),
    "SandboxError": ("chalkcompute._sandbox", "SandboxError"),
    "SandboxInfo": ("chalkcompute._sandbox", "SandboxInfo"),
    "SandboxNotFoundError": ("chalkcompute._sandbox", "SandboxNotFoundError"),
    "SandboxTerminatedError": ("chalkcompute._sandbox", "SandboxTerminatedError"),
    # _scaling_group.py
    "ScalingGroup": ("chalkcompute._scaling_group", "ScalingGroup"),
    "ScalingGroupBuildError": ("chalkcompute._scaling_group", "ScalingGroupBuildError"),
    "ScalingGroupError": ("chalkcompute._scaling_group", "ScalingGroupError"),
    "ScalingGroupInfo": ("chalkcompute._scaling_group", "ScalingGroupInfo"),
    # _volume.py
    "FileInfo": ("chalkcompute._volume", "FileInfo"),
    "Volume": ("chalkcompute._volume", "Volume"),
    "VolumeClient": ("chalkcompute._volume", "VolumeClient"),
    "VolumeError": ("chalkcompute._volume", "VolumeError"),
    "VolumeInfo": ("chalkcompute._volume", "VolumeInfo"),
    "VolumeNotFoundError": ("chalkcompute._volume", "VolumeNotFoundError"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        mod = importlib.import_module(module_path)
        return getattr(mod, attr_name)
    raise AttributeError(f"module 'chalkcompute' has no attribute {name}")


__all__ = list(_LAZY_MAP.keys())
