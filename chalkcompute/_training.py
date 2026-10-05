"""Training function decorator and TrainingFunction class.

Usage:
    import chalkcompute

    @chalkcompute.training(
        data="sms_spam_v3",
        image=Image.debian_slim().pip_install(["torch"]),
        cpu="8",
        memory="32Gi",
        gpu="a10g",
    )
    def train_spam(df, config):
        ...
        return model

    # Remote — provisions a chalkcompute container
    run = train_spam.run_training(config=SpamConfig(lr=1e-3, epochs=10))
    run.wait()

    # Local — runs in-process
    model = train_spam.local(config=SpamConfig(lr=1e-3, epochs=2))
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, overload

from protobuf import Oneof
from protobuf.wkt import Struct, Value

from chalkcompute._connect import ConnectClient, build_image
from chalkcompute._containerspec import ContainerSpec
from chalkcompute._env import normalize_env
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.server.v1 import (
    training_runs_connect as tr_connect,
    training_runs_pb as tr_pb,
)

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

logger = logging.getLogger(__name__)

_CHALK_TRAINING_RUN_ID_ENV = "CHALK_TRAINING_RUN_ID"
_DEFAULT_POLL_INTERVAL = 5.0


class TrainingError(Exception):
    """Raised by TrainingFunction operations."""


def _validate_data_source(
    data: str | None,
    dataset: str | None,
    input_sql: str | None,
) -> None:
    provided = sum(v is not None for v in (data, dataset, input_sql))
    if provided == 0:
        raise TrainingError("exactly one of data=, dataset=, or input_sql= is required")
    if provided > 1:
        raise TrainingError(
            "only one of data=, dataset=, or input_sql= may be provided"
        )


def _is_inside_training_container() -> bool:
    return os.environ.get(_CHALK_TRAINING_RUN_ID_ENV) is not None


def _config_to_struct(config: Any) -> Struct | None:
    if config is None:
        return None
    if isinstance(config, dict):
        return Struct.from_python(config)
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return Struct.from_python(dataclasses.asdict(config))
    raise TrainingError(
        f"config must be a dict or dataclass, got {type(config).__name__}"
    )


def _metadata_to_proto(metadata: dict[str, Any]) -> dict[str, Value]:
    return {k: Value.from_python(v) for k, v in metadata.items()}


def _secret_to_proto(secret: _Secret) -> container_pb.SecretRef:
    source = None
    if secret._integration_name is not None:
        source = Oneof("integration_name", secret._integration_name)
    elif secret._secret_name is not None:
        source = Oneof("secret_name", secret._secret_name)
    return container_pb.SecretRef(
        source=source,  # ty: ignore[invalid-argument-type]
        keys=list(secret._keys),
        aliases=dict(secret._aliases),
        prefix=secret._prefix,
    )


_TERMINAL_STATUSES = frozenset(
    {
        tr_pb.TrainingRunStatus.COMPLETED,
        tr_pb.TrainingRunStatus.FAILED,
        tr_pb.TrainingRunStatus.CANCELED,
    }
)


class TrainingRunHandle:
    """Handle for a submitted training run.

    Returned by :meth:`TrainingFunction.run_training`. Provides polling
    for status, a blocking :meth:`wait` helper, and access to checkpoints.

    Examples
    --------
    >>> handle = train_fn.run_training(config=MyConfig())
    >>> handle.wait().latest_checkpoint()
    """

    def __init__(self, run_id: str, client: ConnectClient) -> None:
        self._run_id = run_id
        self._client = client

    @property
    def run_id(self) -> str:
        return self._run_id

    def status(self) -> tr_pb.TrainingRunStatus:
        """Fetch the current status of this training run."""
        return self._get_run().status

    def _get_run(self) -> tr_pb.TrainingRun:
        stub = self._client.stub(tr_connect.TrainingRunServiceClientSync)
        resp = self._client.call(
            stub.get_training_run,
            tr_pb.GetTrainingRunRequest(training_run_id=self._run_id),
        )
        if resp.training_run is None:
            raise TrainingError(f"training run {self._run_id} not found")
        return resp.training_run

    def wait(
        self,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        timeout: float | None = None,
    ) -> TrainingRunHandle:
        """Block until the training run reaches a terminal state.

        Returns ``self`` for chaining. Raises :class:`TrainingError`
        if the run failed or was canceled, or :class:`TimeoutError`
        if *timeout* seconds elapse first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            run = self._get_run()
            if run.status in _TERMINAL_STATUSES:
                if run.status == tr_pb.TrainingRunStatus.FAILED:
                    raise TrainingError(
                        f"training run {self._run_id} failed: {run.error_message}"
                    )
                if run.status == tr_pb.TrainingRunStatus.CANCELED:
                    raise TrainingError(f"training run {self._run_id} was canceled")
                return self
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"training run {self._run_id} timed out "
                    f"(last status: {run.status.name})"
                )
            time.sleep(poll_interval)

    def latest_checkpoint(self) -> Any | None:
        """Fetch the latest checkpoint for this training run.

        Returns the :class:`ModelArtifact` proto, or ``None`` if no
        checkpoint has been uploaded yet.
        """
        stub = self._client.stub(tr_connect.TrainingRunServiceClientSync)
        resp = self._client.call(
            stub.get_latest_checkpoint,
            tr_pb.GetLatestCheckpointRequest(training_run_id=self._run_id),
        )
        return resp.model_artifact

    def cancel(self) -> None:
        """Cancel this training run."""
        stub = self._client.stub(tr_connect.TrainingRunServiceClientSync)
        self._client.call(
            stub.cancel_training_run,
            tr_pb.CancelTrainingRunRequest(training_run_id=self._run_id),
        )

    def __repr__(self) -> str:
        return f"TrainingRunHandle(run_id={self._run_id!r})"


class TrainingFunction:
    """A training function backed by TrainingRunService.

    Most users should prefer the :func:`training` decorator; use
    ``TrainingFunction`` directly when constructing training runs
    programmatically.

    Unlike :class:`RemoteFunction`, this class does not deploy a scaling
    group or register Arrow schemas.  It stores the decorator arguments
    and a reference to the wrapped callable.  The actual RPC call happens
    in :meth:`run_training`.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        data: str | None = None,
        dataset: str | None = None,
        input_sql: str | None = None,
        image: _Image | None = None,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: Mapping[str, str | _Secret] | None = None,
        secrets: list[_Secret] | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> None:
        _validate_data_source(data, dataset, input_sql)

        self._func = func
        self._func_name = getattr(func, "__name__", "anonymous")

        self._data = data
        self._dataset = dataset
        self._input_sql = input_sql

        env, secrets = normalize_env(env, secrets)
        ContainerSpec.validate_fields(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            secrets=secrets,
        )
        if any(secret.is_lazy for secret in secrets):
            raise ValueError(
                "Training does not support local secrets. "
                "Use Secret.from_chalk_env or Secret.from_chalk_integration instead."
            )
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        self._secrets: list[_Secret] = list(secrets or [])
        self._metadata = dict(metadata) if metadata else {}
        self._max_retries = max_retries

    @property
    def func_name(self) -> str:
        return self._func_name

    @property
    def data_source(self) -> str:
        if self._data is not None:
            return self._data
        if self._dataset is not None:
            return self._dataset
        assert self._input_sql is not None
        return self._input_sql

    @property
    def data_source_type(self) -> str:
        if self._data is not None or self._dataset is not None:
            return "dataset_name"
        return "input_sql"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)

    def local(self, *, config: Any = None) -> Any:
        """Run the training function in-process for local iteration.

        The latest revision of the configured named dataset is downloaded as
        a Polars ``DataFrame`` before the wrapped function is called. No
        training container or offline query is started.

        ``input_sql`` data sources cannot be executed locally and must first
        be materialized as a named dataset.

        Parameters
        ----------
        config
            A dataclass or dict passed to the training function.

        Returns
        -------
        Any
            The value returned by the wrapped training function, typically a
            trained model.
        """
        if self._input_sql is not None:
            raise TrainingError(
                "TrainingFunction.local() cannot execute input_sql locally; "
                "materialize the query as a named dataset and use data= or dataset="
            )

        try:
            from chalk.client import ChalkClient
        except ImportError as exc:
            raise TrainingError(
                "TrainingFunction.local() requires chalkpy with runtime "
                "dependencies; install 'chalkpy[runtime]'"
            ) from exc
        client = ChalkClient()
        dataset = client.get_dataset(dataset_name=self.data_source)
        df = dataset.to_polars()
        return self._func(df, config)

    def _build_deploy_image(self) -> _Image:
        """Build the deploy image with the user's training function source."""
        from chalkcompute._function import (
            _deploy_module_name,
            _make_base_image,
            _package_dir_for_module,
            _source_file_for_deploy,
        )

        from chalkcompute import _local_dev

        config = _local_dev.resolve_local_deploy_config()
        if config.should_mount_sdk_source:
            # Training entrypoint is server-controlled so we can't wrap it
            # to extract an archive (like wrap_entrypoint_for_local_dev does
            # for functions). Instead, add SDK source unarchived to /app so
            # it lands in the same volume as the user's code and is directly
            # importable via PYTHONPATH.
            from chalkcompute._image import Image

            base = self._image if self._image is not None else Image.debian_slim()
            project_root = str(Path(__file__).resolve().parent.parent)
            deploy_image = base.pip_install(
                _local_dev._LOCAL_SDK_RUNTIME_PACKAGES
            ).add_local_dir(
                project_root,
                "/app",
                exclude=_local_dev._LOCAL_SDK_COPY_EXCLUDES,
            )
        else:
            deploy_image = _make_base_image(self._image)

        module_name = _deploy_module_name(self._func)
        if module_name.split(".")[0] == "chalkcompute":
            # A training function shipped with the SDK (e.g. ``chalkcompute.prime_rl``) is
            # already in the image's chalkcompute install; uploading the package would shadow it.
            return deploy_image
        package_dir = _package_dir_for_module(module_name)

        if package_dir is not None:
            top_pkg = module_name.split(".")[0]
            deploy_image = deploy_image.add_local_dir(
                package_dir,
                f"/app/{top_pkg}",
                exclude=["**/__pycache__/**", "**/*.pyc"],
            )
        else:
            source_file = _source_file_for_deploy(self._func)
            target_name = module_name if module_name != "__main__" else self._func_name
            deploy_image = deploy_image.add_local_file(
                source_file, f"/app/{target_name}.py"
            )

        return deploy_image

    def run_training(
        self,
        *,
        config: Any = None,
        client: ConnectClient | None = None,
        experiment_id: str | None = None,
    ) -> TrainingRunHandle:
        """Submit a training run to chalkcompute.

        Parameters
        ----------
        config
            A dataclass or dict passed as the training config. Serialized
            to a ``google.protobuf.Struct`` for the RPC.
        client
            An authenticated :class:`ConnectClient`. One is created
            automatically if not provided.
        experiment_id
            Optional ID of the experiment this training run should be
            associated with.
        """
        if client is None:
            client = ConnectClient()

        deploy_image = self._build_deploy_image()
        image_uri = build_image(client, deploy_image)

        proto_mounts: list[container_pb.VolumeMount] = []
        if deploy_image.lazy_local_files:
            from chalkcompute._volume import VolumeClient

            logger.info(
                "Uploading %d source file(s)...",
                len(deploy_image.lazy_local_files),
            )
            vvclient = VolumeClient.from_connect(client)
            vol_name = f"training-src-{self._name or self._func_name}"
            resolved = vvclient._resolve_lazy_files(
                deploy_image,
                volume_name=vol_name,
            )
            proto_mounts = [
                container_pb.VolumeMount(
                    name=m.name,
                    mount_path=m.mount_path,
                    type="versioned_chalkfs",
                    version_id=m.version_id,
                )
                for m in resolved
            ]

        data_source = self._build_data_source()
        resources = self._build_resources()
        secret_refs = [_secret_to_proto(s) for s in self._secrets]
        meta = _metadata_to_proto(self._metadata) if self._metadata else None

        env = dict(self._env) if self._env else {}
        env["PYTHONPATH"] = "/app"
        module = self._func.__module__
        if module == "__main__":
            import __main__

            spec = getattr(__main__, "__spec__", None)
            if spec and spec.name:
                module = spec.name
            else:
                module = self._func_name
        env["CHALK_TRAINING_MODULE"] = f"{module}.{self._func_name}"

        request = tr_pb.CreateTrainingRunRequest(
            name=self._name,
            data=data_source,
            config=_config_to_struct(config),
            resources=resources,
            image=image_uri,
            env=env or None,
            secret_refs=secret_refs or None,
            meta_data=meta,
            volume_mounts=proto_mounts or None,
            max_retries=self._max_retries,
            experiment_id=experiment_id,
        )

        stub = client.stub(tr_connect.TrainingRunServiceClientSync)
        resp = client.call(stub.create_training_run, request, timeout=60.0)
        if resp.training_run is None:
            raise TrainingError("CreateTrainingRun returned no training run")
        run_id = resp.training_run.id
        logger.info("Created training run %s", run_id)
        return TrainingRunHandle(run_id, client)

    def _build_data_source(self) -> tr_pb.TrainingRunDataSource:
        if self._input_sql is not None:
            source = Oneof("input_sql", self._input_sql)
        else:
            source = Oneof("dataset_name", self._data or self._dataset or "")
        return tr_pb.TrainingRunDataSource(source=source)  # ty: ignore[invalid-argument-type]

    def _build_resources(self) -> container_pb.ResourceLimits | None:
        if self._cpu is None and self._memory is None and self._gpu is None:
            return None
        return container_pb.ResourceLimits(
            cpu=self._cpu,
            memory=self._memory,
            gpu=self._gpu,
        )


class _NoOpTrainingFunction:
    """Returned inside a training container where the decorator is a passthrough."""

    def __init__(self, func: Callable[..., Any]) -> None:
        self._func = func
        self._func_name = getattr(func, "__name__", "anonymous")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)


@overload
def training(
    func: Callable[..., Any],
    /,
    *,
    data: str | None = None,
    dataset: str | None = None,
    input_sql: str | None = None,
    image: _Image | None = None,
    name: str | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> TrainingFunction | _NoOpTrainingFunction: ...


@overload
def training(
    *,
    data: str | None = None,
    dataset: str | None = None,
    input_sql: str | None = None,
    image: _Image | None = None,
    name: str | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> Callable[..., TrainingFunction | _NoOpTrainingFunction]: ...


def training(
    func: Callable[..., Any] | None = None,
    /,
    *,
    data: str | None = None,
    dataset: str | None = None,
    input_sql: str | None = None,
    image: _Image | None = None,
    name: str | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    env: Mapping[str, str | _Secret] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> (
    Callable[..., TrainingFunction | _NoOpTrainingFunction]
    | TrainingFunction
    | _NoOpTrainingFunction
):
    """Decorator that marks a function as a Chalk training function.

    Supports two forms::

        @training(data="my_dataset", image=Image(...), gpu="a10g")
        def train(df, config):
            ...

        @training
        def train(df, config):
            ...

    Inside a training container (detected via ``CHALK_TRAINING_RUN_ID``
    env var), the decorator is a no-op that returns the function body
    unchanged — the entrypoint handles dataset loading and config
    injection.

    ``env`` accepts strings or single-value Chalk Secret references; the mapping
    key sets the injected name, overriding aliases and prefixes. Local env
    secrets are not supported for training.

    Outside a container, returns a :class:`TrainingFunction` that
    exposes ``.run_training()`` for remote execution and ``.local()``
    for in-process iteration.
    """

    def wrap(fn: Callable[..., Any]) -> TrainingFunction | _NoOpTrainingFunction:
        if _is_inside_training_container():
            return _NoOpTrainingFunction(fn)
        return TrainingFunction(
            fn,
            data=data,
            dataset=dataset,
            input_sql=input_sql,
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
            secrets=secrets,
            metadata=metadata,
            max_retries=max_retries,
        )

    if func is not None:
        return wrap(func)
    return wrap
