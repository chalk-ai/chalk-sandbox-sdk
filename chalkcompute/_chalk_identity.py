"""Chalk workload identity configuration shared by every workload surface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from datetime import datetime
from typing import Literal

from chalkcompute._gen.chalk.auth.v1.permissions_pb import Permission

IdentityModeName = Literal["creator", "sandbox"]
_IDENTITY_MODES: tuple[IdentityModeName, ...] = ("creator", "sandbox")


@dataclass(frozen=True, slots=True)
class OIDCAudiencePolicy:
    """Allow any OIDC audience or restrict issuance to an explicit set."""

    _audiences: tuple[str, ...] | None

    @classmethod
    def any(cls) -> OIDCAudiencePolicy:
        """Request unrestricted audiences. Requires an unrestricted ceiling."""
        return cls(None)

    @classmethod
    def only(cls, audiences: Sequence[str]) -> OIDCAudiencePolicy:
        """Allow the specified audiences. An empty sequence denies issuance."""
        if isinstance(audiences, (str, bytes)) or not isinstance(audiences, Sequence):
            raise TypeError("audiences must be a sequence of strings")
        return cls(tuple(audiences))

    def __post_init__(self) -> None:
        if self._audiences is not None:
            if not isinstance(self._audiences, tuple):
                raise TypeError("use OIDCAudiencePolicy.any() or .only(audiences)")
            if any(
                not isinstance(audience, str) or not audience.strip()
                for audience in self._audiences
            ):
                raise ValueError("audiences must be non-empty strings")


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxGroupLimits:
    """Limits requested by the root sandbox for its group.

    The root counts toward the aggregate CPU and memory ceilings but does
    not count as an active descendant. The group deadline uses a timezone-aware
    datetime. The OIDC audience policy bounds issuance for every group member.
    """

    max_active_descendants: int | None = None
    cpu: str | None = None
    memory: str | None = None
    expires_at: datetime | None = None
    oidc_audience_policy: OIDCAudiencePolicy | None = None

    def __post_init__(self) -> None:
        if self.max_active_descendants is not None:
            if type(self.max_active_descendants) is not int:
                raise TypeError("max_active_descendants must be an integer")
            if not 0 <= self.max_active_descendants < 2**32:
                raise ValueError(
                    "max_active_descendants must be between 0 and 2**32 - 1"
                )
        if self.cpu is not None and not isinstance(self.cpu, str):
            raise TypeError("cpu must be a string")
        if self.memory is not None and not isinstance(self.memory, str):
            raise TypeError("memory must be a string")
        if self.expires_at is not None:
            if not isinstance(self.expires_at, datetime):
                raise TypeError("expires_at must be a datetime")
            if self.expires_at.utcoffset() is None:
                raise ValueError("expires_at must be timezone-aware")
        if self.oidc_audience_policy is not None and not isinstance(
            self.oidc_audience_policy, OIDCAudiencePolicy
        ):
            raise TypeError("oidc_audience_policy must be an OIDCAudiencePolicy")


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
    identifies the sandbox. The declaration configures file delivery.
    OIDC issuance is governed by the sandbox's effective audience policy,
    including any inherited limits. Supported on host compute (the sandbox
    default), with at most 8 token files per sandbox. The
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
    permissions
        Chalk API permissions requested for the sandbox principal. The default
        requests an empty grant.
    oidc_audience_policy
        Requested OIDC audience authority. With ``None``, a root uses its
        declared token-file audiences if present and the group ceiling otherwise.
        A child inherits its parent's effective policy.
    user_provided_claims
        String claims requested for the sandbox's Chalk identity token.
    """

    mode: IdentityModeName = "creator"
    _: KW_ONLY
    permissions: Sequence[Permission] = ()
    oidc_audience_policy: OIDCAudiencePolicy | None = None
    user_provided_claims: Mapping[str, str] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.mode not in _IDENTITY_MODES:
            raise ValueError(
                f"ChalkIdentity mode must be one of {list(_IDENTITY_MODES)}, "
                f"got {self.mode!r}"
            )
        if self.mode != "sandbox" and (
            self.permissions
            or self.oidc_audience_policy is not None
            or self.user_provided_claims is not None
        ):
            raise ValueError('principal settings require ChalkIdentity(mode="sandbox")')
        if (
            not isinstance(self.permissions, Sequence)
            or isinstance(self.permissions, (str, bytes))
            or not all(
                isinstance(permission, Permission) for permission in self.permissions
            )
        ):
            raise TypeError("permissions must be a sequence of Permission values")
        object.__setattr__(self, "permissions", tuple(self.permissions))
        if self.oidc_audience_policy is not None and not isinstance(
            self.oidc_audience_policy, OIDCAudiencePolicy
        ):
            raise TypeError("oidc_audience_policy must be an OIDCAudiencePolicy")
        if self.user_provided_claims is not None:
            if not isinstance(self.user_provided_claims, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in self.user_provided_claims.items()
            ):
                raise TypeError("user_provided_claims must map strings to strings")
            object.__setattr__(
                self, "user_provided_claims", dict(self.user_provided_claims)
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
