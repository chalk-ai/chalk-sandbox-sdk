"""Chalkbox — SDK for Chalk sandboxes, containers, and volumes."""

import importlib

_LAZY_MAP: dict[str, tuple[str, str]] = {
    # container.py
    "Container": ("chalkcompute.container", "Container"),
    "ContainerError": ("chalkcompute.container", "ContainerError"),
    "ContainerInfo": ("chalkcompute.container", "ContainerInfo"),
    "ContainerExecResult": ("chalkcompute.container", "ExecResult"),
    "ImageBuildError": ("chalkcompute.container", "ImageBuildError"),
    # function.py
    "RemoteFunction": ("chalkcompute.function", "RemoteFunction"),
    "function": ("chalkcompute.function", "function"),
    # image.py
    "Image": ("chalkcompute.image", "Image"),
    "LazyLocalFile": ("chalkcompute.image", "LazyLocalFile"),
    # sandbox.py
    "ExecError": ("chalkcompute.sandbox", "ExecError"),
    "ExecEvent": ("chalkcompute.sandbox", "ExecEvent"),
    "ExecProcess": ("chalkcompute.sandbox", "ExecProcess"),
    "ExecResult": ("chalkcompute.sandbox", "ExecResult"),
    "Sandbox": ("chalkcompute.sandbox", "Sandbox"),
    "SandboxBuildError": ("chalkcompute.sandbox", "SandboxBuildError"),
    "SandboxClient": ("chalkcompute.sandbox", "SandboxClient"),
    "SandboxError": ("chalkcompute.sandbox", "SandboxError"),
    "SandboxInfo": ("chalkcompute.sandbox", "SandboxInfo"),
    "SandboxNotFoundError": ("chalkcompute.sandbox", "SandboxNotFoundError"),
    "SandboxTerminatedError": ("chalkcompute.sandbox", "SandboxTerminatedError"),
    # scaling_group.py
    "ScalingGroup": ("chalkcompute.scaling_group", "ScalingGroup"),
    "ScalingGroupBuildError": ("chalkcompute.scaling_group", "ScalingGroupBuildError"),
    "ScalingGroupError": ("chalkcompute.scaling_group", "ScalingGroupError"),
    "ScalingGroupInfo": ("chalkcompute.scaling_group", "ScalingGroupInfo"),
    # volume.py
    "FileInfo": ("chalkcompute.volume", "FileInfo"),
    "Volume": ("chalkcompute.volume", "Volume"),
    "VolumeClient": ("chalkcompute.volume", "VolumeClient"),
    "VolumeError": ("chalkcompute.volume", "VolumeError"),
    "VolumeInfo": ("chalkcompute.volume", "VolumeInfo"),
    "VolumeNotFoundError": ("chalkcompute.volume", "VolumeNotFoundError"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        mod = importlib.import_module(module_path)
        return getattr(mod, attr_name)
    raise AttributeError(f"module 'chalkcompute' has no attribute {name}")


__all__ = list(_LAZY_MAP.keys())
