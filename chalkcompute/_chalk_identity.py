"""Chalk workload identity configuration shared by every workload surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

IdentityModeName = Literal["creator", "sandbox"]
_IDENTITY_MODES: tuple[IdentityModeName, ...] = ("creator", "sandbox")


@dataclass(frozen=True)
class OIDCTokenFile:
    """Declare an OIDC token file that the platform provisions and rotates.

    Use it as a value in ``Sandbox(env=...)``. The platform writes a token for
    ``audience`` to a file before your code starts, sets the environment
    variable to the file's path, and replaces the file with a fresh token
    before the old one expires, for the lifetime of the sandbox.

    The descriptor holds no token, client or background task. Consumers must
    reopen the file to pick up a rotated token; a credential that a process
    already read and cached is not refreshed.

    Requires ``chalk_identity=ChalkIdentity(mode="sandbox")``, so the token
    identifies the sandbox rather than the user who created it. The sandbox can
    mint tokens only for the audiences it declares. Supported on host compute
    (the sandbox default), with at most 8 token files per sandbox. The
    variable must not also be set by ``env`` or ``secrets``.

    Parameters
    ----------
    audience
        Audience claim of the token, as expected by the service that verifies
        it, for example ``"snowflakecomputing.com"``.

    Examples
    --------
    >>> import chalkcompute as cc
    >>> sandbox = cc.Sandbox(
    ...     chalk_identity=cc.ChalkIdentity(mode="sandbox"),
    ...     env={
    ...         "SNOWFLAKE_AUTHENTICATOR": "WORKLOAD_IDENTITY",
    ...         "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER": "OIDC",
    ...         "SNOWFLAKE_TOKEN_FILE_PATH": cc.OIDCTokenFile(
    ...             audience="snowflakecomputing.com"
    ...         ),
    ...     },
    ... ).run()
    """

    audience: str

    def __post_init__(self) -> None:
        if not isinstance(self.audience, str) or not self.audience.strip():
            raise ValueError("OIDCTokenFile audience must be a non-empty string")


@dataclass(frozen=True)
class ChalkIdentity:
    """Choose whose identity a workload's Chalk credentials carry.

    ``chalk_identity=True`` is shorthand for ``ChalkIdentity(mode="creator")``.

    Parameters
    ----------
    mode
        ``"creator"`` (default) issues credentials that identify the user or
        service account that created the workload, so every workload that
        principal launches is indistinguishable from it to external services.

        ``"sandbox"`` issues credentials that identify the sandbox itself.
        This is supported for sandboxes only and is required by
        :class:`OIDCTokenFile`.
    """

    mode: IdentityModeName = "creator"

    def __post_init__(self) -> None:
        if self.mode not in _IDENTITY_MODES:
            raise ValueError(
                f"ChalkIdentity mode must be one of {list(_IDENTITY_MODES)}, "
                f"got {self.mode!r}"
            )


def normalize_chalk_identity(
    value: bool | ChalkIdentity | None,
) -> ChalkIdentity | None:
    """Return the identity to configure, or ``None`` when identity is disabled.

    Raises
    ------
    TypeError
        If ``value`` is not a ``bool`` or :class:`ChalkIdentity`.
    """
    if value is None or value is False:
        return None
    if value is True:
        return ChalkIdentity()
    if isinstance(value, ChalkIdentity):
        return value
    raise TypeError(
        f"chalk_identity must be a bool or ChalkIdentity, got {type(value).__name__}"
    )


def validate_chalk_identity(
    value: bool | ChalkIdentity | None,
    *,
    allow_sandbox: bool,
) -> None:
    """Validate ``chalk_identity`` for one workload surface.

    Parameters
    ----------
    value
        The ``chalk_identity`` argument as given by the caller.
    allow_sandbox
        Whether this surface supports ``ChalkIdentity(mode="sandbox")``. Only
        sandboxes do today; functions, classes, containers and scaling groups
        must fail clearly rather than silently run as their creator.

    Raises
    ------
    TypeError
        If ``value`` has the wrong type.
    ValueError
        If sandbox mode is requested on a surface that does not support it.
    """
    identity = normalize_chalk_identity(value)
    if identity is not None and identity.mode == "sandbox" and not allow_sandbox:
        raise ValueError(
            'ChalkIdentity(mode="sandbox") is only supported for sandboxes; '
            "functions, classes, containers and scaling groups run as their creator"
        )


def split_oidc_token_files(
    env: Mapping[str, object] | None,
) -> tuple[dict[str, object], dict[str, OIDCTokenFile]]:
    """Separate :class:`OIDCTokenFile` values from the rest of an ``env`` mapping.

    Returns
    -------
    tuple
        The remaining ``env`` entries and a mapping of environment variable
        name to its :class:`OIDCTokenFile`, both in their original order.
    """
    remaining: dict[str, object] = {}
    files: dict[str, OIDCTokenFile] = {}
    for key, value in (env or {}).items():
        if isinstance(value, OIDCTokenFile):
            files[key] = value
        else:
            remaining[key] = value
    return remaining, files
