"""Retry policy configuration for remote function invocations."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration for handler invocations.

    Construct via the named constructors rather than instantiating directly.
    With exponential backoff, wait times grow geometrically
    (``initial``, ``initial * multiplier``, ``initial * multiplier^2``, ...)
    capped at ``max_wait``. With ``linear``, wait times are constant between
    retries. A plain ``int`` passed to ``function(retries=3)`` is shorthand
    for ``RetryPolicy.exponential(attempts=3)``. By default all ``Exception``
    subclasses trigger a retry; pass a tuple of exception classes to
    ``retry_on`` to narrow the set.

    Attributes
    ----------
    max_retries
        Maximum number of retry attempts (not counting the initial call).
    initial_wait_seconds
        Base wait time in seconds before the first retry.
    backoff_multiplier
        Multiplier applied to wait time on each successive retry.
        Use ``2.0`` for exponential backoff (the default) or ``1.0`` for fixed delay.
    max_wait_seconds
        Upper bound on wait time between retries.
    jitter
        If ``True``, add random jitter to wait times to avoid thundering herd.
    retry_on
        Tuple of exception types that trigger a retry.
        Non-matching exceptions propagate immediately without consuming retry budget.

    Examples
    --------
    >>> RetryPolicy.exponential(attempts=5, initial=1.0, max_wait=30.0)
    >>> RetryPolicy.linear(attempts=3, delay=2.0)
    >>> RetryPolicy.exponential(
    ...     attempts=3,
    ...     retry_on=(ConnectionError, TimeoutError, OSError),
    ... )
    """

    max_retries: int = 3
    initial_wait_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_wait_seconds: float = 60.0
    jitter: bool = True
    retry_on: tuple[type[BaseException], ...] = (Exception,)

    @staticmethod
    def exponential(
        attempts: int = 3,
        *,
        initial: float = 1.0,
        multiplier: float = 2.0,
        max_wait: float = 60.0,
        jitter: bool = True,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
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
            Exception types that trigger a retry.

        Returns
        -------
        RetryPolicy
            A configured ``RetryPolicy``.

        Examples
        --------
        >>> RetryPolicy.exponential(attempts=5, initial=0.5, max_wait=30.0)
        >>> RetryPolicy.exponential(attempts=3, retry_on=(TimeoutError,))
        """
        return RetryPolicy(
            max_retries=attempts,
            initial_wait_seconds=initial,
            backoff_multiplier=multiplier,
            max_wait_seconds=max_wait,
            jitter=jitter,
            retry_on=retry_on,
        )

    @staticmethod
    def linear(
        attempts: int = 3,
        *,
        delay: float = 1.0,
        jitter: bool = False,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
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
            Exception types that trigger a retry.

        Returns
        -------
        RetryPolicy
            A configured ``RetryPolicy``.

        Examples
        --------
        >>> RetryPolicy.linear(attempts=3, delay=2.0)
        >>> RetryPolicy.linear(
        ...     attempts=5,
        ...     delay=0.5,
        ...     retry_on=(ConnectionError, TimeoutError),
        ... )
        """
        return RetryPolicy(
            max_retries=attempts,
            initial_wait_seconds=delay,
            backoff_multiplier=1.0,
            max_wait_seconds=delay,
            jitter=jitter,
            retry_on=retry_on,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for metadata / JSON export.

        The ``retry_on`` field is serialized as a list of exception class names
        only when it differs from the default ``(Exception,)``.

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
        if isinstance(value, int):
            return RetryPolicy.exponential(attempts=value)
        return value
