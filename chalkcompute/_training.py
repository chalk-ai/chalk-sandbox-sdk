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

import logging
import os
from typing import TYPE_CHECKING, Any, Callable, overload

from chalkcompute._containerspec import ContainerSpec

if TYPE_CHECKING:
    from chalkcompute._image import Image as _Image
    from chalkcompute._secret import Secret as _Secret

logger = logging.getLogger(__name__)

_CHALK_TRAINING_RUN_ID_ENV = "CHALK_TRAINING_RUN_ID"


class TrainingError(Exception):
    """Raised by TrainingFunction operations."""


def _validate_data_source(
    data: str | None,
    dataset: str | None,
    input_sql: str | None,
) -> None:
    provided = sum(v is not None for v in (data, dataset, input_sql))
    if provided == 0:
        raise TrainingError(
            "exactly one of data=, dataset=, or input_sql= is required"
        )
    if provided > 1:
        raise TrainingError(
            "only one of data=, dataset=, or input_sql= may be provided"
        )


def _is_inside_training_container() -> bool:
    return os.environ.get(_CHALK_TRAINING_RUN_ID_ENV) is not None


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
        env: dict[str, str] | None = None,
        secrets: list[_Secret] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        _validate_data_source(data, dataset, input_sql)

        self._func = func
        self._func_name = getattr(func, "__name__", "anonymous")

        self._data = data
        self._dataset = dataset
        self._input_sql = input_sql

        ContainerSpec.validate_fields(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env=env,
        )
        self._image = image
        self._name = name
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._env = dict(env) if env else {}
        self._secrets: list[_Secret] = list(secrets or [])
        self._metadata = dict(metadata) if metadata else {}

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

    def local(self, *, config: Any = None, **kwargs: Any) -> Any:
        """Run the training function in-process for local iteration.

        Parameters
        ----------
        config
            A dataclass or dict passed to the training function.
        """
        return self._func(None, config, **kwargs)

    def run_training(self, *, config: Any = None) -> Any:  # noqa: ARG002
        """Submit a training run to chalkcompute.

        Returns a TrainingRunHandle (CHA-10117) that can poll for
        status and stream logs.

        This method is a stub — the RPC integration is CHA-10115.
        """
        raise NotImplementedError(
            "run_training() requires TrainingRunHandle (CHA-10117) "
            "and RPC integration (CHA-10115)"
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
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
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
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
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
    env: dict[str, str] | None = None,
    secrets: list[_Secret] | None = None,
    metadata: dict[str, Any] | None = None,
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
        )

    if func is not None:
        return wrap(func)
    return wrap
