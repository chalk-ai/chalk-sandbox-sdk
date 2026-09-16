"""Shared validation and serialization for Chalk container specifications."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from enum import Enum
from typing import Any, cast

from protobuf import Oneof
from protobuf.wkt import Duration

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
    """

    host: str
    private_key_secret: str
    _: KW_ONLY
    host_keys: Sequence[str] | None = None
    port: int | None = None
    username: str | None = None

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
        headers_secrets: dict[str, str] = field(default_factory=dict)

        def __post_init__(self) -> None:
            self.headers = dict(self.headers or {})
            for key, value in self.headers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("headers must be a dict[str, str]")
            self.headers_secrets = dict(self.headers_secrets or {})
            for key, value in self.headers_secrets.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("headers_secrets must be a dict[str, str]")

        def to_dict(self) -> dict[str, Any]:
            body: dict[str, Any] = {"headers": dict(self.headers)}
            if self.headers_secrets:
                body["headersSecrets"] = dict(self.headers_secrets)
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
    managed_ssh: Mapping[str, SSHDest] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    protocol: str | None = None
    authentication: str | None = None
    security_policy: ContainerSecurityPolicy | None = None
    network_policy: NetworkPolicy | None = None
    compute_class: ComputeClass | None = None
    startup_probe: Mapping[str, Any] | None = None
    readiness_probe: Mapping[str, Any] | None = None
    restart_policy: RestartPolicy | None = None
    chalk_identity: bool = False

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
            tags=self.tags,
            restart_policy=self.restart_policy,
            chalk_identity=self.chalk_identity,
            managed_ssh=self.managed_ssh,
        )
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
        object.__setattr__(self, "tags", dict(self.tags))
        object.__setattr__(
            self,
            "managed_ssh",
            dict(self.managed_ssh),
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
        tags: Mapping[str, str] | None = None,
        restart_policy: RestartPolicy | None = None,
        chalk_identity: bool = False,
        managed_ssh: Mapping[str, SSHDest] | None = None,
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
        if tags is not None:
            if not isinstance(tags, Mapping):
                raise TypeError("tags must be a dict[str, str]")
            for key, value in tags.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("tags must be a dict[str, str]")
        if restart_policy is not None and not isinstance(restart_policy, RestartPolicy):
            raise TypeError("restart_policy must be a RestartPolicy")
        if not isinstance(chalk_identity, bool):
            raise TypeError("chalk_identity must be a bool")
        if managed_ssh is not None and not isinstance(managed_ssh, Mapping):
            raise TypeError("managed_ssh must be a mapping of aliases to destinations")
        if managed_ssh is not None:
            for alias, destination in managed_ssh.items():
                if not isinstance(alias, str):
                    raise TypeError("managed_ssh aliases must be strings")
                if not alias:
                    raise ValueError("managed_ssh aliases must not be empty")
                if not isinstance(destination, SSHDest):
                    raise TypeError(
                        "managed_ssh destinations must be SSHDest instances"
                    )

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
                for alias, destination in self.managed_ssh.items()
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
        if self.startup_probe is not None:
            spec["startupProbe"] = dict(self.startup_probe)
        if self.readiness_probe is not None:
            spec["readinessProbe"] = dict(self.readiness_probe)
        if self.restart_policy is not None:
            spec["restartPolicy"] = self.restart_policy.value
        if self.chalk_identity:
            spec["chalkWorkloadIdentity"] = {}
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
                    type="versioned_chalkfs",
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
                for alias, destination in self.managed_ssh.items()
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
            startup_probe=startup_probe,
            readiness_probe=readiness_probe,
            restart_policy=(
                None
                if self.restart_policy is None
                else container_pb.RestartPolicy[self.restart_policy.name]
            ),
            chalk_workload_identity=(
                container_pb.ChalkWorkloadIdentity() if self.chalk_identity else None
            ),
        )


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
        source=source,  # ty: ignore[invalid-argument-type]
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
    return container_pb.NetworkPolicyMatcher(
        matcher=value  # ty: ignore[invalid-argument-type]
    )


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
                                headers_secrets=dict(transformer.headers_secrets),
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
