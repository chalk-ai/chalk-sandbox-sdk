"""Shared validation and serialization for Chalk container specifications."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from enum import Enum
from typing import Any, cast

from protobuf import Oneof
from protobuf.wkt import Duration

from chalkcompute._chalk_identity import (
    ChalkIdentity,
    OIDCTokenFile,
    normalize_chalk_identity,
    validate_chalk_identity,
)
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._image import Image
from chalkcompute._naming import validate_dns_label, validate_env
from chalkcompute._secret import Secret
from chalkcompute._volume import VolumeMount


class KernelPolicy(Enum):
    RESTRICTED = "RESTRICTED"
    OPEN = "OPEN"


class ComputeClass(Enum):
    K8S = "COMPUTE_CLASS_K8S"
    HOST = "COMPUTE_CLASS_HOST"


class ExecutionBackend(Enum):
    """Cluster placement for a new workload, independent of its compute class."""

    ENVIRONMENT = "EXECUTION_BACKEND_ENVIRONMENT"
    SERVERLESS = "EXECUTION_BACKEND_SERVERLESS"


class ResourceDetector(Enum):
    """Egress detectors that record resources a sandbox creates.

    Each detector inspects the sandbox's egress traffic to the hosts it
    recognizes. A sandbox that enables a detector implicitly allows those
    hosts in its ``NetworkPolicy``.
    """

    GITHUB_PULL_REQUESTS = "RESOURCE_DETECTOR_GITHUB_PULL_REQUESTS"

    @property
    def hosts(self) -> tuple[str, ...]:
        """Hosts whose egress traffic this detector inspects."""
        return _RESOURCE_DETECTOR_HOSTS[self]


_RESOURCE_DETECTOR_HOSTS = {
    ResourceDetector.GITHUB_PULL_REQUESTS: ("api.github.com",),
}


class RestartPolicy(Enum):
    """Policy controlling whether a sandbox restarts after exiting."""

    NEVER = "RESTART_POLICY_NEVER"
    ALWAYS = "RESTART_POLICY_ALWAYS"


@dataclass(frozen=True, slots=True)
class SSHDest:
    """An SSH destination exposed inside a sandbox under a managed alias.

    Attributes
    ----------
    host
        DNS hostname or IP address authorized for SSH and used as ``HostName``.
    private_key_secret
        Name of the Chalk secret containing the SSH private key.
    host_keys
        SSH host key entries. ``None`` or an empty sequence uses trust on first
        use for the sandbox lifetime.
    port
        SSH destination port. Defaults to 22.
    username
        Optional SSH username written to the generated alias's ``User`` setting.
    alias
        Name the destination is reachable by inside the sandbox, as in
        ``ssh <alias>``. Used when ``managed_ssh`` is a list; defaults to
        ``host``. A ``managed_ssh`` mapping's keys take precedence.
    """

    host: str
    private_key_secret: str
    _: KW_ONLY
    host_keys: Sequence[str] | None = None
    port: int | None = None
    username: str | None = None
    alias: str | None = None

    @classmethod
    def github(
        cls,
        private_key_secret: str,
        *,
        alias: str = "github.com",
        port_443: bool = False,
    ) -> SSHDest:
        """GitHub over SSH, pinned to GitHub's published host keys.

        ``git@github.com:org/repo.git`` URLs work unchanged inside the sandbox.

        Parameters
        ----------
        private_key_secret
            Name of the Chalk secret containing a key registered with GitHub,
            such as a deploy key or a user's SSH key.
        alias
            SSH alias for the destination. Use distinct aliases to reach
            GitHub with more than one key, as in
            ``git@github-work:org/repo.git``.
        port_443
            Connect to ``ssh.github.com:443`` instead of ``github.com:22``,
            for networks that block port 22.

        Example
        -------
        ```python
        import chalkcompute as cc

        sandbox = cc.Sandbox(
            compute_class=cc.ComputeClass.HOST,
            managed_ssh=[cc.SshDest.github("GITHUB_DEPLOY_KEY")],
        ).run()
        sandbox.git.clone(url="git@github.com:org/repo.git", path="/workspace/repo")
        ```
        """
        return cls(
            host="ssh.github.com" if port_443 else "github.com",
            private_key_secret=private_key_secret,
            host_keys=GITHUB_SSH_HOST_KEYS,
            port=443 if port_443 else None,
            username="git",
            alias=alias,
        )

    def __post_init__(self) -> None:
        for name, value in (
            ("host", self.host),
            ("private_key_secret", self.private_key_secret),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.host_keys is not None:
            if isinstance(self.host_keys, str) or not all(
                isinstance(host_key, str) for host_key in self.host_keys
            ):
                raise TypeError("host_keys must be a sequence of strings or None")
            object.__setattr__(self, "host_keys", tuple(self.host_keys))
        if self.port is not None:
            _validate_port(self.port, "port")
        if self.username is not None and not isinstance(self.username, str):
            raise TypeError("username must be a string")
        if self.alias is not None:
            _validate_ssh_alias(self.alias)

    def to_dict(self) -> dict[str, Any]:
        destination: dict[str, Any] = {
            "host": self.host,
            "privateKeySecret": self.private_key_secret,
        }
        if self.host_keys:
            destination["hostKeys"] = list(self.host_keys)
        if self.port is not None:
            destination["port"] = self.port
        if self.username is not None:
            destination["username"] = self.username
        return destination


# Preserve the original spelling for backwards compatibility.
SshDest = SSHDest


# GitHub's SSH host keys, from https://api.github.com/meta. ssh.github.com:443
# serves the same keys.
GITHUB_SSH_HOST_KEYS = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=",
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=",
)


def _validate_ssh_alias(alias: object) -> None:
    if not isinstance(alias, str):
        raise TypeError("managed_ssh aliases must be strings")
    if not alias:
        raise ValueError("managed_ssh aliases must not be empty")


def normalize_managed_ssh(
    managed_ssh: Mapping[str, SSHDest] | Sequence[SSHDest] | None,
) -> dict[str, SSHDest]:
    """Validate ``managed_ssh`` and return it keyed by alias.

    A list keys each destination by its ``alias``, falling back to ``host``.
    """
    if managed_ssh is None:
        return {}
    if isinstance(managed_ssh, Mapping):
        items = list(managed_ssh.items())
    elif isinstance(managed_ssh, (str, bytes)) or not isinstance(managed_ssh, Sequence):
        raise TypeError(
            "managed_ssh must be a list of SSHDest or a mapping of aliases to SSHDest"
        )
    else:
        items = []
        for destination in managed_ssh:
            if not isinstance(destination, SSHDest):
                raise TypeError("managed_ssh destinations must be SSHDest instances")
            items.append((destination.alias or destination.host, destination))
    result: dict[str, SSHDest] = {}
    for alias, destination in items:
        _validate_ssh_alias(alias)
        if not isinstance(destination, SSHDest):
            raise TypeError("managed_ssh destinations must be SSHDest instances")
        if alias in result:
            raise ValueError(
                f"managed_ssh has more than one destination for alias {alias!r}; "
                "give each a distinct alias="
            )
        result[alias] = destination
    return result


@dataclass
class ContainerSecurityPolicy:
    """Security policy settings for a container."""

    kernel_policy: KernelPolicy = KernelPolicy.RESTRICTED

    def to_dict(self) -> dict[str, Any]:
        return {"kernelPolicy": f"KERNEL_POLICY_{self.kernel_policy.value}"}


_PortRangeInput = tuple[int, int] | int


class NetworkPolicy:
    """Network egress policy for a container."""

    @dataclass(init=False)
    class Route:
        route: str
        ports: list[tuple[int, int]]

        def __init__(
            self,
            route: str,
            *,
            ports: Iterable[_PortRangeInput] | None = None,
        ) -> None:
            if not isinstance(route, str):
                raise TypeError("route must be a string")
            if not route:
                raise ValueError("route must not be empty")
            self.route = route
            self.ports = [_coerce_port_range(port) for port in ports or []]

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {"route": self.route}
            if self.ports:
                body["portRanges"] = [
                    {"startPort": start_port, "endPort": end_port}
                    for start_port, end_port in self.ports
                ]
            return body

    @dataclass
    class Matcher:
        exact: str | None = None
        starts_with: str | None = None
        regex: str | None = None

        def __post_init__(self) -> None:
            values = [
                self.exact is not None,
                self.starts_with is not None,
                self.regex is not None,
            ]
            if sum(values) != 1:
                raise ValueError("set exactly one of exact, starts_with, or regex")
            for name, value in (
                ("exact", self.exact),
                ("starts_with", self.starts_with),
                ("regex", self.regex),
            ):
                if value is not None and not isinstance(value, str):
                    raise TypeError(f"{name} must be a string")

        def to_dict(self) -> dict[str, str]:
            if self.exact is not None:
                return {"exact": self.exact}
            if self.starts_with is not None:
                return {"startsWith": self.starts_with}
            assert self.regex is not None
            return {"regex": self.regex}

    @dataclass
    class KeyValueMatcher:
        key: NetworkPolicy.Matcher
        value: NetworkPolicy.Matcher | None = None

        def __post_init__(self) -> None:
            if not isinstance(self.key, NetworkPolicy.Matcher):
                raise TypeError("key must be a NetworkPolicy.Matcher")
            if self.value is not None and not isinstance(
                self.value, NetworkPolicy.Matcher
            ):
                raise TypeError("value must be a NetworkPolicy.Matcher")

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {"key": self.key.to_dict()}
            if self.value is not None:
                body["value"] = self.value.to_dict()
            return body

    @dataclass
    class Match:
        path: NetworkPolicy.Matcher | None = None
        methods: list[str] = field(default_factory=list)
        query_strings: list[NetworkPolicy.KeyValueMatcher] = field(default_factory=list)
        headers: list[NetworkPolicy.KeyValueMatcher] = field(default_factory=list)

        def __post_init__(self) -> None:
            if self.path is not None and not isinstance(
                self.path, NetworkPolicy.Matcher
            ):
                raise TypeError("path must be a NetworkPolicy.Matcher")
            if isinstance(self.methods, str):
                raise TypeError("methods must be a list of strings")
            self.methods = list(self.methods or [])
            for method in self.methods:
                if not isinstance(method, str):
                    raise TypeError("methods must be a list of strings")
            self.query_strings = list(self.query_strings or [])
            for matcher in self.query_strings:
                if not isinstance(matcher, NetworkPolicy.KeyValueMatcher):
                    raise TypeError(
                        "query_strings must be a list of NetworkPolicy.KeyValueMatcher"
                    )
            self.headers = list(self.headers or [])
            for matcher in self.headers:
                if not isinstance(matcher, NetworkPolicy.KeyValueMatcher):
                    raise TypeError(
                        "headers must be a list of NetworkPolicy.KeyValueMatcher"
                    )

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {}
            if self.path is not None:
                body["path"] = self.path.to_dict()
            if self.methods:
                body["method"] = list(self.methods)
            if self.query_strings:
                body["queryString"] = [
                    matcher.to_dict() for matcher in self.query_strings
                ]
            if self.headers:
                body["headers"] = [matcher.to_dict() for matcher in self.headers]
            return body

    @dataclass
    class Transformer:
        headers: dict[str, str] = field(default_factory=dict)
        headers_secrets: dict[str, str | Secret] = field(default_factory=dict)

        def __post_init__(self) -> None:
            self.headers = dict(self.headers or {})
            for key, value in self.headers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("headers must be a dict[str, str]")
            self.headers_secrets = dict(self.headers_secrets or {})
            for key, value in self.headers_secrets.items():
                if not isinstance(key, str) or not isinstance(value, (str, Secret)):
                    raise TypeError("headers_secrets must be a dict[str, str | Secret]")

        def _serialized_headers_secrets(self) -> dict[str, str]:
            serialized: dict[str, str] = {}
            for placeholder, secret in self.headers_secrets.items():
                if isinstance(secret, str):
                    serialized[placeholder] = secret
                elif secret._secret_name is not None:
                    serialized[placeholder] = secret._secret_name
                else:
                    raise ValueError(
                        "network policy secrets must resolve to a single Chalk "
                        "secret before serialization"
                    )
            return serialized

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {"headers": dict(self.headers)}
            if self.headers_secrets:
                body["headersSecrets"] = self._serialized_headers_secrets()
            return body

    @dataclass
    class Rule:
        transforms: list[NetworkPolicy.Transformer] = field(default_factory=list)
        match: NetworkPolicy.Match | None = None
        forward_url: str | None = None

        def __post_init__(self) -> None:
            self.transforms = list(self.transforms or [])
            for transformer in self.transforms:
                if not isinstance(transformer, NetworkPolicy.Transformer):
                    raise TypeError(
                        "transforms must be a list of NetworkPolicy.Transformer"
                    )
            if self.match is not None and not isinstance(
                self.match, NetworkPolicy.Match
            ):
                raise TypeError("match must be a NetworkPolicy.Match")
            if self.forward_url is not None and not isinstance(self.forward_url, str):
                raise TypeError("forward_url must be a string")

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {}
            if self.transforms:
                body["transform"] = [
                    transformer.to_dict() for transformer in self.transforms
                ]
            if self.match is not None:
                body["match"] = self.match.to_dict()
            if self.forward_url is not None:
                body["forwardUrl"] = self.forward_url
            return body

    def __init__(
        self,
        *,
        allowed_routes: Iterable[Route] | None = None,
        denied_routes: Iterable[str] | None = None,
        allowed_hosts: Iterable[str] | Mapping[str, Iterable[Rule]] | None = None,
    ) -> None:
        self.allowed_routes = list(allowed_routes or [])
        for route in self.allowed_routes:
            if not isinstance(route, NetworkPolicy.Route):
                raise TypeError("allowed_routes must be a list of NetworkPolicy.Route")

        if isinstance(denied_routes, str):
            raise TypeError("denied_routes must be a list of strings")
        self.denied_routes = list(denied_routes or [])
        for route in self.denied_routes:
            if not isinstance(route, str):
                raise TypeError("denied_routes must be a list of strings")

        self.allowed_hosts: dict[str, list[NetworkPolicy.Rule]] = {}
        if allowed_hosts is None:
            return
        if isinstance(allowed_hosts, Mapping):
            for host, rules in allowed_hosts.items():
                if not isinstance(host, str):
                    raise TypeError("allowed_hosts entries must be strings")
                self._validate_host(host)
                if isinstance(rules, NetworkPolicy.Rule):
                    raise TypeError(
                        "allowed_hosts values must be lists of NetworkPolicy.Rule"
                    )
                self.allowed_hosts[host] = list(
                    cast(Iterable[NetworkPolicy.Rule], rules)
                )
                for rule in self.allowed_hosts[host]:
                    if not isinstance(rule, NetworkPolicy.Rule):
                        raise TypeError(
                            "allowed_hosts values must be lists of NetworkPolicy.Rule"
                        )
            return
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a list of strings or a dict")
        for host in allowed_hosts:
            self._validate_host(host)
            self.allowed_hosts[host] = []

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "allowedRoutes": [route.to_dict() for route in self.allowed_routes]
        }
        if self.denied_routes:
            body["deniedRoutes"] = list(self.denied_routes)
        if self.allowed_hosts:
            body["allowedHosts"] = {
                host: {"rules": [rule.to_dict() for rule in rules]}
                for host, rules in self.allowed_hosts.items()
            }
        return body

    @staticmethod
    def _validate_host(host: str) -> None:
        if not isinstance(host, str):
            raise TypeError("allowed_hosts entries must be strings")
        if not host:
            raise ValueError("allowed_hosts entries must not be empty")


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Normalized SDK representation of ``ChalkContainerSpec``."""

    image: str
    name: str | None = None
    entrypoint: Sequence[str] | None = None
    cpu: str | None = None
    memory: str | None = None
    gpu: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    volumes: Sequence[VolumeMount] = ()
    secret_refs: Sequence[Secret | Mapping[str, Any]] = ()
    port: int | None = None
    lifetime: str | None = None
    managed_ssh: Mapping[str, SSHDest] | Sequence[SSHDest] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    protocol: str | None = None
    authentication: str | None = None
    security_policy: ContainerSecurityPolicy | None = None
    network_policy: NetworkPolicy | None = None
    compute_class: ComputeClass | None = None
    execution_target: ExecutionBackend | None = None
    resource_detectors: Sequence[ResourceDetector] = ()
    host_pool_id: str | None = None
    startup_probe: Mapping[str, Any] | None = None
    readiness_probe: Mapping[str, Any] | None = None
    restart_policy: RestartPolicy | None = None
    chalk_identity: bool | ChalkIdentity = False
    # Environment variable name -> token file the platform provisions and rotates.
    oidc_token_files: Mapping[str, OIDCTokenFile] = field(default_factory=dict)
    # Only sandboxes can run as their own principal; every other surface leaves
    # this False so ChalkIdentity(mode="sandbox") fails at construction.
    allow_sandbox_identity: bool = False

    def __post_init__(self) -> None:
        self.validate_fields(
            image=self.image,
            name=self.name,
            cpu=self.cpu,
            memory=self.memory,
            gpu=self.gpu,
            env=self.env,
            entrypoint=self.entrypoint,
            secrets=self.secret_refs,
            port=self.port,
            lifetime=self.lifetime,
            security_policy=self.security_policy,
            network_policy=self.network_policy,
            compute_class=self.compute_class,
            execution_target=self.execution_target,
            resource_detectors=self.resource_detectors,
            tags=self.tags,
            restart_policy=self.restart_policy,
            chalk_identity=self.chalk_identity,
            oidc_token_files=self.oidc_token_files,
            allow_sandbox_identity=self.allow_sandbox_identity,
            managed_ssh=self.managed_ssh,
        )
        object.__setattr__(self, "oidc_token_files", dict(self.oidc_token_files))
        object.__setattr__(
            self,
            "entrypoint",
            None if self.entrypoint is None else tuple(self.entrypoint),
        )
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "volumes", tuple(self.volumes))
        object.__setattr__(
            self,
            "secret_refs",
            tuple(
                ref if isinstance(ref, Secret) else dict(ref)
                for ref in self.secret_refs
            ),
        )
        object.__setattr__(self, "resource_detectors", tuple(self.resource_detectors))
        object.__setattr__(self, "tags", dict(self.tags))
        object.__setattr__(
            self,
            "managed_ssh",
            normalize_managed_ssh(self.managed_ssh),
        )
        object.__setattr__(
            self,
            "startup_probe",
            None if self.startup_probe is None else dict(self.startup_probe),
        )
        object.__setattr__(
            self,
            "readiness_probe",
            None if self.readiness_probe is None else dict(self.readiness_probe),
        )

    @staticmethod
    def validate_fields(
        *,
        image: object | None = None,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        env: Mapping[str, str] | None = None,
        entrypoint: Sequence[str] | None = None,
        secrets: Sequence[Secret | Mapping[str, Any]] | None = None,
        port: int | None = None,
        lifetime: str | None = None,
        security_policy: ContainerSecurityPolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        compute_class: ComputeClass | None = None,
        execution_target: ExecutionBackend | None = None,
        resource_detectors: Sequence[ResourceDetector] | None = None,
        tags: Mapping[str, str] | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool | ChalkIdentity = False,
        oidc_token_files: Mapping[str, OIDCTokenFile] | None = None,
        allow_sandbox_identity: bool = False,
        managed_ssh: Mapping[str, SSHDest] | Sequence[SSHDest] | None = None,
    ) -> None:
        if image is not None and not isinstance(image, str):
            if not isinstance(image, Image):
                raise TypeError(
                    f"image must be an Image or image reference string, got {image!r}"
                )
        if name is not None:
            validate_dns_label(name)
        for field_name, value, example in (
            ("cpu", cpu, "'2' or '500m'"),
            ("memory", memory, "'2Gi' or '512Mi'"),
            ("gpu", gpu, "'nvidia-l4'"),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"{field_name} must be a string such as {example}, got {value!r}"
                )
        validate_env(env)
        if entrypoint is not None:
            if isinstance(entrypoint, str) or not all(
                isinstance(part, str) for part in entrypoint
            ):
                raise TypeError("entrypoint must be a list of strings")
        if secrets is not None:
            if isinstance(secrets, (str, bytes)) or not isinstance(secrets, Sequence):
                raise TypeError("secrets must be a list of Secret")
            for secret in secrets:
                if isinstance(secret, Mapping):
                    continue
                if not isinstance(secret, Secret):
                    raise TypeError("secrets must be a list of Secret")
        if port is not None and not (1 <= port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {port!r}")
        if lifetime is not None:
            try:
                parsed_lifetime = Duration.from_json(f'"{lifetime}"')
            except ValueError:
                raise ValueError(
                    f"Invalid lifetime {lifetime!r}. "
                    "Use a protobuf duration string (e.g. '30s', '1.5s', '300s')."
                ) from None
            if parsed_lifetime.seconds == 0 and parsed_lifetime.nanos == 0:
                raise ValueError(
                    f"Invalid lifetime {lifetime!r}: lifetime must be greater than 0 "
                    "(e.g. '30s', '5m')."
                )
        if security_policy is not None and not isinstance(
            security_policy, ContainerSecurityPolicy
        ):
            raise TypeError("security_policy must be a ContainerSecurityPolicy")
        if network_policy is not None and not isinstance(network_policy, NetworkPolicy):
            raise TypeError("network_policy must be a NetworkPolicy")
        if compute_class is not None and not isinstance(compute_class, ComputeClass):
            raise TypeError("compute_class must be a ComputeClass")
        if execution_target is not None and not isinstance(
            execution_target, ExecutionBackend
        ):
            raise TypeError("execution_target must be an ExecutionBackend")
        if resource_detectors is not None:
            if isinstance(resource_detectors, (str, bytes)) or not isinstance(
                resource_detectors, Sequence
            ):
                raise TypeError("resource_detectors must be a list of ResourceDetector")
            for detector in resource_detectors:
                if not isinstance(detector, ResourceDetector):
                    raise TypeError(
                        "resource_detectors must be a list of ResourceDetector"
                    )
        if tags is not None:
            if not isinstance(tags, Mapping):
                raise TypeError("tags must be a dict[str, str]")
            for key, value in tags.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("tags must be a dict[str, str]")
        if restart_policy is not None and not isinstance(restart_policy, RestartPolicy):
            raise TypeError("restart_policy must be a RestartPolicy")
        validate_chalk_identity(chalk_identity, allow_sandbox=allow_sandbox_identity)
        _validate_oidc_token_files(
            oidc_token_files,
            chalk_identity=chalk_identity,
            allow_sandbox_identity=allow_sandbox_identity,
            env=env,
            compute_class=compute_class,
        )
        normalize_managed_ssh(managed_ssh)

    def to_dict(self) -> dict[str, Any]:
        spec: dict[str, Any] = {"image": self.image}
        if self.name is not None:
            spec["name"] = self.name
        if self.entrypoint is not None:
            spec["entrypoint"] = list(self.entrypoint)
        resources = {
            key: value
            for key, value in (
                ("cpu", self.cpu),
                ("memory", self.memory),
                ("gpu", self.gpu),
            )
            if value is not None
        }
        if resources:
            spec["resources"] = resources
        if self.env:
            spec["envVars"] = dict(self.env)
        if self.volumes:
            spec["volumes"] = [volume.to_spec_dict() for volume in self.volumes]
        if self.secret_refs:
            spec["secretRefs"] = [
                ref._to_proto_dict() if isinstance(ref, Secret) else dict(ref)
                for ref in self.secret_refs
            ]
        if self.port is not None:
            spec["port"] = self.port
        if self.lifetime is not None:
            spec["lifetime"] = self.lifetime
        if self.managed_ssh:
            spec["managedSsh"] = {
                alias: destination.to_dict()
                for alias, destination in normalize_managed_ssh(
                    self.managed_ssh
                ).items()
            }
        if self.tags:
            spec["tags"] = dict(self.tags)
        if self.protocol is not None:
            spec["protocol"] = self.protocol
        if self.authentication is not None:
            spec["authentication"] = self.authentication
        if self.security_policy is not None:
            spec["securityPolicy"] = self.security_policy.to_dict()
        if self.network_policy is not None:
            spec["networkPolicy"] = self.network_policy.to_dict()
        if self.compute_class is not None:
            spec["computeClass"] = self.compute_class.value
        if self.execution_target is not None:
            spec["executionTarget"] = {"backend": self.execution_target.value}
        if self.host_pool_id is not None:
            spec["hostPlacement"] = {"hostPoolId": self.host_pool_id}
        if self.startup_probe is not None:
            spec["startupProbe"] = dict(self.startup_probe)
        if self.readiness_probe is not None:
            spec["readinessProbe"] = dict(self.readiness_probe)
        if self.restart_policy is not None:
            spec["restartPolicy"] = self.restart_policy.value
        identity = normalize_chalk_identity(self.chalk_identity)
        if identity is not None:
            spec["chalkWorkloadIdentity"] = (
                {
                    "mode": "IDENTITY_MODE_WORKLOAD_PRINCIPAL",
                    **(
                        {
                            "oidcTokenFiles": [
                                {"audience": file.audience, "envVar": env_var}
                                for env_var, file in self.oidc_token_files.items()
                            ]
                        }
                        if self.oidc_token_files
                        else {}
                    ),
                }
                if identity.mode == "sandbox"
                else {"mode": "IDENTITY_MODE_CREATOR_IDENTITY"}
            )
        return spec

    def to_proto(self) -> container_pb.ChalkContainerSpec:
        resources = None
        if self.cpu is not None or self.memory is not None or self.gpu is not None:
            resources = container_pb.ResourceLimits(
                cpu=self.cpu,
                memory=self.memory,
                gpu=self.gpu,
            )

        secret_refs = [
            _secret_to_proto(ref)
            if isinstance(ref, Secret)
            else container_pb.SecretRef.from_json(json.dumps(ref))
            for ref in self.secret_refs
        ]
        startup_probe = (
            None
            if self.startup_probe is None
            else container_pb.StartupProbe.from_json(json.dumps(self.startup_probe))
        )
        readiness_probe = (
            None
            if self.readiness_probe is None
            else container_pb.ReadinessProbe.from_json(json.dumps(self.readiness_probe))
        )
        return container_pb.ChalkContainerSpec(
            image=self.image,
            name=self.name or "",
            entrypoint=list(self.entrypoint or ()),
            resources=resources,
            env_vars=dict(self.env),
            volumes=[
                container_pb.VolumeMount(
                    name=volume.name,
                    mount_path=volume.mount_path,
                    type=volume.type.value,
                    size_limit=volume.size_limit,
                    version_id=volume.version_id,
                    ref_name=volume.ref_name,
                )
                for volume in self.volumes
            ],
            secret_refs=secret_refs,
            port=self.port,
            lifetime=(
                None
                if self.lifetime is None
                else Duration.from_json(json.dumps(self.lifetime))
            ),
            managed_ssh={
                alias: container_pb.ManagedSshDestination(
                    host=destination.host,
                    private_key_secret=destination.private_key_secret,
                    host_keys=list(destination.host_keys or ()),
                    port=destination.port,
                    username=destination.username,
                )
                for alias, destination in normalize_managed_ssh(
                    self.managed_ssh
                ).items()
            },
            tags=dict(self.tags),
            protocol=self.protocol,
            authentication=self.authentication,
            security_policy=(
                None
                if self.security_policy is None
                else container_pb.ContainerSecurityPolicy(
                    kernel_policy=container_pb.KernelPolicy[
                        self.security_policy.kernel_policy.name
                    ]
                )
            ),
            network_policy=(
                None
                if self.network_policy is None
                else _network_policy_to_proto(self.network_policy)
            ),
            compute_class=(
                None
                if self.compute_class is None
                else container_pb.ComputeClass[self.compute_class.name]
            ),
            execution_target=(
                None
                if self.execution_target is None
                else container_pb.ExecutionTarget(
                    backend=container_pb.ExecutionBackend[self.execution_target.name]
                )
            ),
            resource_detection=(
                container_pb.ResourceDetectionConfig(
                    detectors=[
                        container_pb.ResourceDetector[detector.name]
                        for detector in self.resource_detectors
                    ]
                )
                if self.resource_detectors
                else None
            ),
            placement=(
                None
                if self.host_pool_id is None
                else Oneof(
                    "host_placement",
                    container_pb.HostPlacementOptions(host_pool_id=self.host_pool_id),
                )
            ),
            startup_probe=startup_probe,
            readiness_probe=readiness_probe,
            restart_policy=(
                None
                if self.restart_policy is None
                else container_pb.RestartPolicy[self.restart_policy.name]
            ),
            chalk_workload_identity=self._workload_identity_proto(),
        )

    def _workload_identity_proto(self) -> container_pb.ChalkWorkloadIdentity | None:
        identity = normalize_chalk_identity(self.chalk_identity)
        if identity is None:
            return None
        if identity.mode == "creator":
            return container_pb.ChalkWorkloadIdentity(
                mode=container_pb.IdentityMode.CREATOR_IDENTITY
            )
        return container_pb.ChalkWorkloadIdentity(
            mode=container_pb.IdentityMode.WORKLOAD_PRINCIPAL,
            workload_principal=container_pb.WorkloadPrincipalIdentity(),
            oidc_token_files=[
                container_pb.OIDCTokenFile(audience=file.audience, env_var=env_var)
                for env_var, file in self.oidc_token_files.items()
            ],
        )


# Injected by the platform next to the Chalk identity token; an OIDCTokenFile
# cannot reuse them.
_RESERVED_IDENTITY_ENV_VARS = frozenset(
    {"CHALK_WEB_IDENTITY_TOKEN_FILE", "CHALK_API_SERVER", "CHALK_ENVIRONMENT"}
)
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The platform re-mints every declared token concurrently on each refresh, so it
# caps the declarations per sandbox.
_MAX_OIDC_TOKEN_FILES = 8


def _validate_oidc_token_files(
    oidc_token_files: Mapping[str, OIDCTokenFile] | None,
    *,
    chalk_identity: bool | ChalkIdentity,
    allow_sandbox_identity: bool,
    env: Mapping[str, str] | None,
    compute_class: ComputeClass | None,
) -> None:
    """Check OIDC token file declarations the same way the server will.

    Variables injected by ``secrets`` are checked by the server, which knows
    which keys an integration holds only after resolving it.
    """
    if not oidc_token_files:
        return
    if not allow_sandbox_identity:
        raise ValueError("OIDCTokenFile is only supported in the env of a Sandbox")
    identity = normalize_chalk_identity(chalk_identity)
    if identity is None or identity.mode != "sandbox":
        raise ValueError(
            'OIDCTokenFile requires chalk_identity=ChalkIdentity(mode="sandbox") '
            "so the token identifies the sandbox rather than its creator"
        )
    # Sandboxes default to host compute; only an explicit K8S choice is refused.
    if compute_class is not None and compute_class != ComputeClass.HOST:
        raise ValueError(
            f"OIDCTokenFile requires host compute; got compute_class={compute_class}"
        )
    if len(oidc_token_files) > _MAX_OIDC_TOKEN_FILES:
        raise ValueError(
            f"at most {_MAX_OIDC_TOKEN_FILES} OIDCTokenFile values may be declared, "
            f"got {len(oidc_token_files)}"
        )
    for env_var, file in oidc_token_files.items():
        if not isinstance(file, OIDCTokenFile):
            raise TypeError(
                f"oidc_token_files[{env_var!r}] must be an OIDCTokenFile, got "
                f"{type(file).__name__}"
            )
        if not _ENV_VAR_NAME_RE.match(env_var):
            raise ValueError(
                f"OIDCTokenFile env var {env_var!r} is not a valid environment "
                "variable name"
            )
        if env_var in _RESERVED_IDENTITY_ENV_VARS:
            raise ValueError(
                f"OIDCTokenFile env var {env_var!r} is reserved for Chalk workload "
                "identity"
            )
        if env and env_var in env:
            raise ValueError(f"environment variable {env_var!r} is already set")


def validate_replica_and_utilization(
    *,
    min_replicas: int,
    max_replicas: int,
    gpu: str | None = None,
    target_cpu_utilization_percentage: int | None = None,
    target_gpu_utilization_percentage: int | None = None,
) -> None:
    """Validate replica counts and CPU/GPU utilization triggers.

    Shared by :class:`~chalkcompute.ScalingGroup`,
    :class:`~chalkcompute.RemoteFunction`, and
    :class:`~chalkcompute.RemoteClass`.
    """
    if min_replicas < 0:
        raise ValueError(f"min_replicas must be >= 0, got {min_replicas}")
    if max_replicas < 0:
        raise ValueError(f"max_replicas must be >= 0, got {max_replicas}")
    if min_replicas > max_replicas:
        raise ValueError(
            f"min_replicas ({min_replicas}) must be <= max_replicas ({max_replicas})"
        )
    if target_cpu_utilization_percentage is not None and not (
        0 < target_cpu_utilization_percentage <= 100
    ):
        raise ValueError(
            "target_cpu_utilization_percentage must be between 1 and 100, "
            f"got {target_cpu_utilization_percentage}"
        )
    if target_gpu_utilization_percentage is not None:
        if not (0 < target_gpu_utilization_percentage <= 100):
            raise ValueError(
                "target_gpu_utilization_percentage must be between 1 and 100, "
                f"got {target_gpu_utilization_percentage}"
            )
        if gpu is None:
            raise ValueError("target_gpu_utilization_percentage requires gpu to be set")
        # The utilization series only exists while replicas do, so a group that
        # may reach zero would deactivate and never see a sample to scale back up.
        if min_replicas < 1 and max_replicas != 0:
            raise ValueError(
                "target_gpu_utilization_percentage requires min_replicas >= 1, "
                f"got {min_replicas}"
            )


def _secret_to_proto(secret: Secret) -> container_pb.SecretRef:
    source = None
    if secret._integration_name is not None:
        source = Oneof("integration_name", secret._integration_name)
    elif secret._secret_name is not None:
        source = Oneof("secret_name", secret._secret_name)
    return container_pb.SecretRef(
        source=source,
        keys=list(secret._keys),
        aliases=dict(secret._aliases),
        prefix=secret._prefix,
    )


def _network_matcher_to_proto(
    matcher: NetworkPolicy.Matcher,
) -> container_pb.NetworkPolicyMatcher:
    if matcher.exact is not None:
        value = Oneof("exact", matcher.exact)
    elif matcher.starts_with is not None:
        value = Oneof("starts_with", matcher.starts_with)
    else:
        assert matcher.regex is not None
        value = Oneof("regex", matcher.regex)
    return container_pb.NetworkPolicyMatcher(matcher=value)


def _network_key_value_matcher_to_proto(
    matcher: NetworkPolicy.KeyValueMatcher,
) -> container_pb.NetworkPolicyKeyValueMatcher:
    return container_pb.NetworkPolicyKeyValueMatcher(
        key=_network_matcher_to_proto(matcher.key),
        value=(
            None if matcher.value is None else _network_matcher_to_proto(matcher.value)
        ),
    )


def _network_policy_to_proto(policy: NetworkPolicy) -> container_pb.NetworkPolicy:
    return container_pb.NetworkPolicy(
        allowed_routes=[
            container_pb.AllowedRoute(
                route=route.route,
                port_ranges=[
                    container_pb.PortRange(
                        start_port=start_port,
                        end_port=end_port,
                    )
                    for start_port, end_port in route.ports
                ],
            )
            for route in policy.allowed_routes
        ],
        denied_routes=list(policy.denied_routes),
        allowed_hosts={
            host: container_pb.NetworkPolicyRuleList(
                rules=[
                    container_pb.NetworkPolicyRule(
                        transform=[
                            container_pb.NetworkTransformer(
                                headers=dict(transformer.headers),
                                headers_secrets=transformer._serialized_headers_secrets(),
                            )
                            for transformer in rule.transforms
                        ],
                        match=(
                            None
                            if rule.match is None
                            else container_pb.NetworkPolicyMatch(
                                path=(
                                    None
                                    if rule.match.path is None
                                    else _network_matcher_to_proto(rule.match.path)
                                ),
                                method=list(rule.match.methods),
                                query_string=[
                                    _network_key_value_matcher_to_proto(matcher)
                                    for matcher in rule.match.query_strings
                                ],
                                headers=[
                                    _network_key_value_matcher_to_proto(matcher)
                                    for matcher in rule.match.headers
                                ],
                            )
                        ),
                        forward_url=rule.forward_url,
                    )
                    for rule in rules
                ]
            )
            for host, rules in policy.allowed_hosts.items()
        },
    )


def _coerce_port_range(port_range: _PortRangeInput) -> tuple[int, int]:
    if isinstance(port_range, int) and not isinstance(port_range, bool):
        _validate_port_range(port_range, port_range)
        return port_range, port_range
    if isinstance(port_range, tuple) and len(port_range) == 2:
        start_port, end_port = port_range
        _validate_port_range(start_port, end_port)
        return start_port, end_port
    raise TypeError("ports must be integers or (start_port, end_port) tuples")


def _validate_port_range(start_port: int, end_port: int) -> None:
    _validate_port(start_port, "start_port")
    _validate_port(end_port, "end_port")
    if end_port < start_port:
        raise ValueError("end_port must be greater than or equal to start_port")


def _validate_port(port: int, name: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"{name} must be an integer")
    if port < 1 or port > 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
