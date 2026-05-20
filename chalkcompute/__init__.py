"""Chalkbox — SDK for Chalk sandboxes, containers, and volumes."""

import importlib

from chalkcompute._logging import setup_logging as _setup_logging

_setup_logging()

# Keep top-level imports lazy so `import chalkcompute` does not eagerly import
# the grpc/protobuf/client modules. Update `__init__.pyi` when adding exports.
_LAZY_MAP: dict[str, tuple[str, str]] = {
    # _container.py
    "Container": ("chalkcompute._container", "Container"),
    "ContainerError": ("chalkcompute._container", "ContainerError"),
    "ContainerInfo": ("chalkcompute._container", "ContainerInfo"),
    "ContainerSecurityPolicy": ("chalkcompute._container", "ContainerSecurityPolicy"),
    "ContainerExecResult": ("chalkcompute._container", "ExecResult"),
    "AllowedRoute": ("chalkcompute._container", "AllowedRoute"),
    "ImageBuildError": ("chalkcompute._container", "ImageBuildError"),
    "KernelPolicy": ("chalkcompute._container", "KernelPolicy"),
    "NetworkPolicy": ("chalkcompute._container", "NetworkPolicy"),
    "PortRange": ("chalkcompute._container", "PortRange"),
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
    "FunctionCallHandle": ("chalkcompute._function", "FunctionCallHandle"),
    "FunctionVersionInfo": ("chalkcompute._function", "FunctionVersionInfo"),
    "RetryPolicy": ("chalkcompute._retry", "RetryPolicy"),
    "RateLimitPolicy": ("chalkcompute._rate_limit", "RateLimitPolicy"),
    "ConcurrencyPolicy": ("chalkcompute._concurrency", "ConcurrencyPolicy"),
    "QueuePolicy": ("chalkcompute._quota", "QueuePolicy"),
    "TracingPolicy": (
        "chalkcompute._remote_function_tracing",
        "TracingPolicy",
    ),
    "function": ("chalkcompute._function", "function"),
    # _model.py
    "bind_model": ("chalkcompute._model", "bind_model"),
    "ModelError": ("chalkcompute._model", "ModelError"),
    "ModelNotFoundError": ("chalkcompute._model", "ModelNotFoundError"),
    "ModelNotDeployedError": ("chalkcompute._model", "ModelNotDeployedError"),
    # _connect.py
    "ConnectClient": ("chalkcompute._connect", "ConnectClient"),
    "build_image": ("chalkcompute._connect", "build_image_from_spec"),
    "build_image_with_volumes": (
        "chalkcompute._connect",
        "build_image_with_volumes_from_spec",
    ),
    # _tracing.py
    "span": ("chalkcompute._tracing", "span"),
    "traced": ("chalkcompute._tracing", "traced"),
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
    "VolumeClosedError": ("chalkcompute._volume", "VolumeClosedError"),
    "VolumeError": ("chalkcompute._volume", "VolumeError"),
    "VolumeFileNotFoundError": ("chalkcompute._volume", "VolumeFileNotFoundError"),
    "VolumeInfo": ("chalkcompute._volume", "VolumeInfo"),
    "VolumeNotFoundError": ("chalkcompute._volume", "VolumeNotFoundError"),
    # _vvolume.py
    "VersionInfo": ("chalkcompute._vvolume", "VersionInfo"),
    "VersionedVolume": ("chalkcompute._vvolume", "VersionedVolume"),
    "VersionedVolumeClient": ("chalkcompute._vvolume", "VersionedVolumeClient"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        mod = importlib.import_module(module_path)
        return getattr(mod, attr_name)
    raise AttributeError(f"module 'chalkcompute' has no attribute {name}")


__all__ = list(_LAZY_MAP.keys())
