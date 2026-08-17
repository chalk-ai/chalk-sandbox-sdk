"""Chalkbox — SDK for Chalk sandboxes, containers, and volumes."""

import importlib

from chalkcompute._logging import setup_logging as _setup_logging

_setup_logging()

# Keep top-level imports lazy so `import chalkcompute` does not eagerly import
# the grpc/protobuf/client modules. Update `__init__.pyi` when adding exports.
_LAZY_MAP: dict[str, tuple[str, str]] = {
    # _artifact.py
    "Artifact": ("chalkcompute._artifact", "Artifact"),
    "ArtifactClient": ("chalkcompute._artifact", "ArtifactClient"),
    "ArtifactError": ("chalkcompute._artifact", "ArtifactError"),
    "ArtifactNotFoundError": ("chalkcompute._artifact", "ArtifactNotFoundError"),
    # _container.py
    "Container": ("chalkcompute._container", "Container"),
    "ContainerError": ("chalkcompute._container", "ContainerError"),
    "ContainerInfo": ("chalkcompute._container", "ContainerInfo"),
    "ContainerSession": ("chalkcompute._container", "ContainerSession"),
    "ContainerSecurityPolicy": ("chalkcompute._container", "ContainerSecurityPolicy"),
    "ContainerExecResult": ("chalkcompute._container", "ExecResult"),
    "ImageBuildError": ("chalkcompute._container", "ImageBuildError"),
    "KernelPolicy": ("chalkcompute._container", "KernelPolicy"),
    "ComputeClass": ("chalkcompute._container", "ComputeClass"),
    "NetworkPolicy": ("chalkcompute._container", "NetworkPolicy"),
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
    "gather": ("chalkcompute._function", "gather"),
    "RetryPolicy": ("chalkcompute._retry", "RetryPolicy"),
    "RateLimitPolicy": ("chalkcompute._rate_limit", "RateLimitPolicy"),
    "ConcurrencyPolicy": ("chalkcompute._concurrency", "ConcurrencyPolicy"),
    "QueuePolicy": ("chalkcompute._quota", "QueuePolicy"),
    "TracingPolicy": (
        "chalkcompute._remote_function_tracing",
        "TracingPolicy",
    ),
    "function": ("chalkcompute._function", "function"),
    # _evaluation.py
    "DatasetNameLike": ("chalkcompute._evaluation", "DatasetNameLike"),
    "DatasetRevisionLike": ("chalkcompute._evaluation", "DatasetRevisionLike"),
    "DatasetRevisionRef": ("chalkcompute._evaluation", "DatasetRevisionRef"),
    "Evaluation": ("chalkcompute._evaluation", "Evaluation"),
    "EvaluationClient": ("chalkcompute._evaluation", "EvaluationClient"),
    "EvaluationError": ("chalkcompute._evaluation", "EvaluationError"),
    "EvaluationNotFoundError": (
        "chalkcompute._evaluation",
        "EvaluationNotFoundError",
    ),
    "EvaluationRun": ("chalkcompute._evaluation", "EvaluationRun"),
    "EvaluationRunFailedError": (
        "chalkcompute._evaluation",
        "EvaluationRunFailedError",
    ),
    "EvaluationRunStatus": ("chalkcompute._evaluation", "EvaluationRunStatus"),
    "EvaluationScorer": ("chalkcompute._evaluation", "EvaluationScorer"),
    # _remote_call_client.py
    "RemoteCallClient": ("chalkcompute._remote_call_client", "RemoteCallClient"),
    "RemoteCallClientUnavailable": (
        "chalkcompute._remote_call_client",
        "RemoteCallClientUnavailable",
    ),
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
    "trace_headers": ("chalkcompute._tracing", "trace_headers"),
    "traced": ("chalkcompute._tracing", "traced"),
    # _image.py
    "Image": ("chalkcompute._image", "Image"),
    "LazyLocalFile": ("chalkcompute._image", "LazyLocalFile"),
    # _secret.py
    "Secret": ("chalkcompute._secret", "Secret"),
    "SecretScope": ("chalkcompute._secret", "SecretScope"),
    "resolve_lazy_secrets": ("chalkcompute._secret", "resolve_lazy_secrets"),
    # _sandbox.py
    "ExecError": ("chalkcompute._sandbox", "ExecError"),
    "ExecEvent": ("chalkcompute._sandbox", "ExecEvent"),
    "ExecProcess": ("chalkcompute._sandbox", "ExecProcess"),
    "ExecResult": ("chalkcompute._sandbox", "ExecResult"),
    "PythonExecution": ("chalkcompute._sandbox", "PythonExecution"),
    "PythonSkippedArtifact": ("chalkcompute._sandbox", "PythonSkippedArtifact"),
    "RestartPolicy": ("chalkcompute._sandbox", "RestartPolicy"),
    "Sandbox": ("chalkcompute._sandbox", "Sandbox"),
    "SandboxBuildError": ("chalkcompute._sandbox", "SandboxBuildError"),
    "SandboxCreationError": ("chalkcompute._sandbox", "SandboxCreationError"),
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
    "ReadinessProbe": ("chalkcompute._scaling_group", "ReadinessProbe"),
    "CronScalingTrigger": ("chalkcompute._scaling_group", "CronScalingTrigger"),
    "CronScalingWindow": ("chalkcompute._scaling_group", "CronScalingWindow"),
    # _volume.py
    "FileInfo": ("chalkcompute._volume", "FileInfo"),
    "ForkInfo": ("chalkcompute._volume", "ForkInfo"),
    "VersionInfo": ("chalkcompute._volume", "VersionInfo"),
    "Volume": ("chalkcompute._volume", "Volume"),
    "VolumeClient": ("chalkcompute._volume", "VolumeClient"),
    "VolumeClosedError": ("chalkcompute._volume", "VolumeClosedError"),
    "VolumeError": ("chalkcompute._volume", "VolumeError"),
    "VolumeFileNotFoundError": ("chalkcompute._volume", "VolumeFileNotFoundError"),
    "VolumeInfo": ("chalkcompute._volume", "VolumeInfo"),
    "VolumeMount": ("chalkcompute._volume", "VolumeMount"),
    "VolumeNotFoundError": ("chalkcompute._volume", "VolumeNotFoundError"),
    # Imported by chalkpy's model-deploy path; every volume mounts as
    # MOUNT_TYPE, so this only validates existence now.
    "resolve_referenced_volume_type": (
        "chalkcompute._volume",
        "resolve_referenced_volume_type",
    ),
    # Back-compat aliases: all volumes are versioned, so the qualifier is gone
    # from the canonical names. Keep the old spellings importable.
    "VersionedVolume": ("chalkcompute._volume", "Volume"),
    "VersionedVolumeClient": ("chalkcompute._volume", "VolumeClient"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        mod = importlib.import_module(module_path)
        return getattr(mod, attr_name)
    raise AttributeError(f"module 'chalkcompute' has no attribute {name}")


__all__ = list(_LAZY_MAP.keys())
