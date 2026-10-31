"""Retry policy configuration for remote function invocations."""

from __future__ import annotations

import dataclasses
import math


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration for handler invocations.

    Construct via the named constructors rather than instantiating directly.
    With exponential backoff, wait times grow geometrically
    (``initial``, ``initial * multiplier``, ``initial * multiplier^2``, ...)
    capped at ``max_wait``. With ``linear``, wait times are constant between
    retries. A plain ``int`` passed to ``function(retries=3)`` is shorthand
    for ``RetryPolicy.exponential(attempts=3)``. By default all ``Exception``
    subclasses trigger a retry. Direct ``remote()`` calls enforce backoff and
    jitter in the client. Queued calls enforce only ``max_retries`` with a fixed
    pending-entry reclaim delay. Exception filters and shared retry keys are
    reserved and rejected when configuring or invoking a function.

    Examples
    --------
    >>> RetryPolicy.exponential(attempts=5, initial=1.0, max_wait=30.0)
    >>> RetryPolicy.linear(attempts=3, delay=2.0)
    """

    max_retries: int = 3
    """Maximum number of retry attempts (not counting the initial call)."""

    initial_wait_seconds: float = 1.0
    """Base wait time in seconds before the first retry."""

    backoff_multiplier: float = 2.0
    """Multiplier applied to wait time on each successive retry. Use ``2.0`` for
    exponential backoff (the default) or ``1.0`` for fixed delay."""

    max_wait_seconds: float = 60.0
    """Upper bound on wait time between retries."""

    jitter: bool = True
    """If true, uniformly sample each wait between zero and its backoff cap."""

    retry_on: tuple[type[BaseException], ...] = (Exception,)
    """Reserved exception filter. Only the default ``(Exception,)`` is supported."""

    key: str | None = None
    """Reserved shared-bucket identifier. Only ``None`` is supported."""

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise TypeError("max_retries must be an int")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.max_retries > 2**31 - 1:
            raise ValueError("max_retries must fit in an int32")
        for name in ("initial_wait_seconds", "max_wait_seconds", "backoff_multiplier"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        if self.max_wait_seconds < self.initial_wait_seconds:
            raise ValueError("max_wait_seconds must be >= initial_wait_seconds")
        if not isinstance(self.jitter, bool):
            raise TypeError("jitter must be a bool")
        if not isinstance(self.retry_on, tuple) or not all(
            isinstance(exc, type) and issubclass(exc, Exception)
            for exc in self.retry_on
        ):
            raise TypeError("retry_on must be a tuple of Exception classes")
        if self.key is not None and not isinstance(self.key, str):
            raise TypeError("key must be a string or None")

    def validate_execution(self) -> None:
        """Reject policy fields that invocation runtimes cannot enforce."""
        if self.retry_on != (Exception,):
            raise ValueError("retry_on exception filters are not supported yet")
        if self.key is not None:
            raise ValueError("shared retry keys are not supported yet")

    @staticmethod
    def from_proto(value: object) -> RetryPolicy:
        """Restore deployed defaults, including defaults omitted by older deployments."""
        from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb

        if not isinstance(value, service_pb.RetryPolicy):
            raise TypeError("Expected a deployed RetryPolicy")
        return RetryPolicy(
            max_retries=value.max_retries,
            initial_wait_seconds=1.0
            if not value.has_field("initial_backoff_ms")
            else value.initial_backoff_ms / 1000,
            backoff_multiplier=2.0
            if not value.has_field("backoff_multiplier")
            else value.backoff_multiplier,
            max_wait_seconds=60.0
            if not value.has_field("max_backoff_ms")
            else value.max_backoff_ms / 1000,
            jitter=True if not value.has_field("jitter") else value.jitter,
            key=value.key if value.has_field("key") and value.key else None,
        )

    @staticmethod
    def exponential(
        attempts: int = 3,
        *,
        initial: float = 1.0,
        multiplier: float = 2.0,
        max_wait: float = 60.0,
        jitter: bool = True,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
        key: str | None = None,
    ) -> RetryPolicy:
        """Create a policy with exponential backoff.

        Parameters
        ----------
        attempts
            Maximum number of retry attempts.
        initial
            Base wait time in seconds before the first retry.
        multiplier
            Multiplier applied to wait time on each successive retry.
        max_wait
            Upper bound on wait time between retries.
        jitter
            If ``True``, add random jitter to wait times.
        retry_on
            Reserved exception filter; only ``(Exception,)`` is supported.

        Returns
        -------
        RetryPolicy
            A configured ``RetryPolicy``.

        Examples
        --------
        >>> RetryPolicy.exponential(attempts=5, initial=0.5, max_wait=30.0)
        """
        return RetryPolicy(
            max_retries=attempts,
            initial_wait_seconds=initial,
            backoff_multiplier=multiplier,
            max_wait_seconds=max_wait,
            jitter=jitter,
            retry_on=retry_on,
            key=key,
        )

    @staticmethod
    def linear(
        attempts: int = 3,
        *,
        delay: float = 1.0,
        jitter: bool = False,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
        key: str | None = None,
    ) -> RetryPolicy:
        """Create a policy with fixed delay between retries.

        Parameters
        ----------
        attempts
            Maximum number of retry attempts.
        delay
            Fixed wait time in seconds between retries.
        jitter
            If ``True``, add random jitter to wait times.
        retry_on
            Reserved exception filter; only ``(Exception,)`` is supported.

        Returns
        -------
        RetryPolicy
            A configured ``RetryPolicy``.

        Examples
        --------
        >>> RetryPolicy.linear(attempts=3, delay=2.0)
        """
        return RetryPolicy(
            max_retries=attempts,
            initial_wait_seconds=delay,
            backoff_multiplier=1.0,
            max_wait_seconds=delay,
            jitter=jitter,
            retry_on=retry_on,
            key=key,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for metadata / JSON export.

        The ``retry_on`` field is serialized as a list of exception class names
        only when it differs from the default ``(Exception,)``. The ``key``
        field is omitted when ``None``.

        Returns
        -------
        dict[str, object]
            A JSON-serializable dict representation of the policy.
        """
        d: dict[str, object] = {
            "max_retries": self.max_retries,
            "initial_wait_seconds": self.initial_wait_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "max_wait_seconds": self.max_wait_seconds,
            "jitter": self.jitter,
        }
        if self.retry_on != (Exception,):
            d["retry_on"] = [exc.__name__ for exc in self.retry_on]
        if self.key is not None:
            d["key"] = self.key
        return d

    @staticmethod
    def from_int_or_policy(value: int | RetryPolicy) -> RetryPolicy:
        """Normalize ``int | RetryPolicy`` into a ``RetryPolicy``.

        An ``int`` is treated as ``RetryPolicy.exponential(attempts=value)``.

        Parameters
        ----------
        value
            Either a max-attempts integer shorthand or a pre-built policy.

        Returns
        -------
        RetryPolicy
            The normalized policy.
        """
        if isinstance(value, bool):
            raise TypeError("Expected int or RetryPolicy, got bool")
        if isinstance(value, int):
            return RetryPolicy.exponential(attempts=value)
        if not isinstance(value, RetryPolicy):
            raise TypeError("Expected int or RetryPolicy")
        return value
