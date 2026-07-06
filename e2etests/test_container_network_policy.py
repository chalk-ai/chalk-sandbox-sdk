"""End-to-end tests for container network policies."""

from __future__ import annotations

import socket
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pytest

from chalkcompute import ComputeClass, Container, ContainerError, NetworkPolicy
from e2etests.conftest import CHALK_API_SERVER, needs_staging

NETSHOOT_IMAGE = "nicolaka/netshoot:latest"
HTTP_CONNECT_TIMEOUT = 20
HTTP_MAX_TIME = 60
DENY_HTTP_CONNECT_TIMEOUT = 2
DENY_HTTP_MAX_TIME = 5
if ".meta.ci.chalk.dev" in CHALK_API_SERVER:
    INTERNAL_FIXTURE_SUFFIX = "netpol-e2e.demo.internal.aws.chalk.dev"
else:
    INTERNAL_FIXTURE_SUFFIX = "staging.internal.gcp.chalk.dev"
POLICY_READY_DOMAIN = "policy-ready." + INTERNAL_FIXTURE_SUFFIX
POLICY_READY_URL = "http://" + POLICY_READY_DOMAIN + "/status/200"
LOCAL_ECHO_PORT = 8080
LOCAL_ECHO_ENTRYPOINT = f"""set -e
PYTHON=$(command -v python3 || command -v python)
"$PYTHON" - <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "\\n".join(
            f"{{key.lower()}}: {{value}}" for key, value in self.headers.items()
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", {LOCAL_ECHO_PORT}), Handler).serve_forever()
PY
sleep 600
"""

CIDR_ALLOWED_DOMAIN = "cidr-allowed." + INTERNAL_FIXTURE_SUFFIX
CIDR_BLOCKED_DOMAIN = "cidr-blocked." + INTERNAL_FIXTURE_SUFFIX
HOST_ROUTE_DENIED_DOMAIN = "host-route-denied." + INTERNAL_FIXTURE_SUFFIX

ALLOWED_EXACT_DOMAIN = "allowed." + INTERNAL_FIXTURE_SUFFIX
ALLOWED_WILDCARD_SUFFIX = "allowed-wildcard." + INTERNAL_FIXTURE_SUFFIX
ALLOWED_WILDCARD_CHILD_DOMAIN = "child." + ALLOWED_WILDCARD_SUFFIX
BLOCKED_DOMAIN = "blocked." + INTERNAL_FIXTURE_SUFFIX

DOMAIN_FRONTING_SNI = "fronting-allowed." + INTERNAL_FIXTURE_SUFFIX
DOMAIN_FRONTING_BLOCKED_HOST = "fronting-blocked." + INTERNAL_FIXTURE_SUFFIX
JUKED_DESTINATION_DOMAIN = "juked." + INTERNAL_FIXTURE_SUFFIX

MATCH_DOMAIN = "match." + INTERNAL_FIXTURE_SUFFIX
TRANSFORM_DOMAIN = "transform." + INTERNAL_FIXTURE_SUFFIX
FORWARD_DOMAIN = "forward." + INTERNAL_FIXTURE_SUFFIX
MATCH_FORWARD_DOMAIN = "match-forward." + INTERNAL_FIXTURE_SUFFIX
TRANSFORM_FORWARD_DOMAIN = "transform-forward." + INTERNAL_FIXTURE_SUFFIX
MATCH_TRANSFORM_FORWARD_DOMAIN = (
    "match-transform-forward." + INTERNAL_FIXTURE_SUFFIX
)

DOMAIN_ALLOW_PATTERNS = [
    ALLOWED_EXACT_DOMAIN,
    "*." + ALLOWED_WILDCARD_SUFFIX,
]
ALLOWED_DOMAINS = [
    ALLOWED_EXACT_DOMAIN,
    ALLOWED_WILDCARD_CHILD_DOMAIN,
]
TRANSFORM_HEADER_VALUE = "transformed-by-envoy"
TRANSFORM_OVERWRITE_VALUE = "overwritten-by-envoy"
FORWARD_HEADER_VALUE = "forwarded-by-envoy"

POLICY_CASE_NAMES = [
    "allowall",
    "denyall",
    "subnets-only",
    "domains-only",
    "domains-and-subnets",
    "juked-destination",
    "domain-fronting",
    "deny-route-over-allowed-route",
    "deny-route-over-allowed-host",
    "match-rules",
    "forward-rules",
    "transform-rules",
    "rule-combinations",
]
FORWARDING_POLICY_CASE_NAMES = [
    "forward-rules",
    "transform-rules",
    "rule-combinations",
]
ROUTE_BLOCK_READY_CASE_NAMES = {
    "denyall",
    "subnets-only",
    "deny-route-over-allowed-route",
    "deny-route-over-allowed-host",
}
NON_FORWARDING_POLICY_CASE_NAMES = [
    case_name
    for case_name in POLICY_CASE_NAMES
    if case_name not in FORWARDING_POLICY_CASE_NAMES
]


@dataclass(frozen=True)
class CidrProbe:
    cidr: str
    target: str
    host: str


@dataclass(frozen=True)
class PolicyTargets:
    allowed_cidr_probes: list[CidrProbe]
    blocked_cidr_probes: list[CidrProbe]
    denied_allowed_cidr_probes: list[CidrProbe]
    denied_allowed_host_probe: CidrProbe
    blocked_domains: list[str]
    juked_destination_ip: str
    transform_connect_ip: str


@dataclass(frozen=True)
class DnsProbe:
    name: str
    category: str
    expected_ok: bool
    domain: str


@dataclass(frozen=True)
class HttpProbe:
    name: str
    kind: str
    category: str
    expected_ok: bool
    url: str
    method: str = "GET"
    header: str | None = None
    connect_to: str | None = None
    pass_mode: str = "firewall"
    body_contains: str | None = None


@dataclass(frozen=True)
class PolicyCase:
    name: str
    policy: NetworkPolicy
    probes: list[DnsProbe | HttpProbe]
    local_echo: bool = False


@dataclass(frozen=True)
class ProbeRecord:
    name: str
    expected_ok: bool
    ok: bool
    exit_code: int
    target: str
    status: str | None = None
    stderr: str = ""
    body_sample: str = ""


@pytest.fixture(scope="module")
def policy_targets() -> PolicyTargets:
    allowed_cidr_probe = _resolve_cidr_probe(CIDR_ALLOWED_DOMAIN)
    blocked_cidr_probe = _resolve_cidr_probe(CIDR_BLOCKED_DOMAIN)
    denied_allowed_host_probe = _resolve_cidr_probe(HOST_ROUTE_DENIED_DOMAIN)
    fixture_ips = {
        allowed_cidr_probe.target,
        blocked_cidr_probe.target,
        denied_allowed_host_probe.target,
    }
    if len(fixture_ips) != 3:
        raise RuntimeError(
            "internal network-policy CIDR fixtures must resolve to distinct IPv4 "
            "addresses: "
            + ", ".join(
                [
                    allowed_cidr_probe.host + "=" + allowed_cidr_probe.target,
                    blocked_cidr_probe.host + "=" + blocked_cidr_probe.target,
                    denied_allowed_host_probe.host
                    + "="
                    + denied_allowed_host_probe.target,
                ]
            )
        )
    return PolicyTargets(
        allowed_cidr_probes=[allowed_cidr_probe],
        blocked_cidr_probes=[blocked_cidr_probe],
        denied_allowed_cidr_probes=[blocked_cidr_probe],
        denied_allowed_host_probe=denied_allowed_host_probe,
        blocked_domains=[BLOCKED_DOMAIN],
        juked_destination_ip=blocked_cidr_probe.target,
        transform_connect_ip=denied_allowed_host_probe.target,
    )


@needs_staging
def test_host_container_network_policy(
    policy_targets: PolicyTargets,
) -> None:
    _assert_policy_cases(NON_FORWARDING_POLICY_CASE_NAMES, policy_targets)


@needs_staging
@pytest.mark.xfail(
    reason=(
        "forward_url is rendered consistently with the hypervisor e2e suite, "
        "but SDK HOST containers do not currently expose workload localhost "
        "to Envoy"
    ),
    strict=True,
)
def test_host_container_network_policy_forwarding(
    policy_targets: PolicyTargets,
) -> None:
    _assert_policy_cases(FORWARDING_POLICY_CASE_NAMES, policy_targets)


def _assert_policy_cases(
    case_names: list[str],
    policy_targets: PolicyTargets,
) -> None:
    failures_by_case: dict[str, list[str]] = {}

    with ThreadPoolExecutor(max_workers=len(case_names)) as executor:
        futures = {}
        for case_name in case_names:
            futures[
                executor.submit(
                    _run_policy_case,
                    _build_policy_case(case_name, policy_targets),
                )
            ] = case_name

        for future in as_completed(futures):
            case_name = futures[future]
            try:
                failures = future.result()
            except BaseException as exc:
                failures_by_case[case_name] = [
                    "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ).rstrip()
                ]
                continue
            if failures:
                failures_by_case[case_name] = failures

    assert not failures_by_case, _format_case_failures(failures_by_case)


def _run_policy_case(case: PolicyCase) -> list[str]:
    print(f"[{case.name}] starting", flush=True)
    container = _run_netshoot_container(
        f"test-network-policy-{case.name}",
        network_policy=case.policy,
        local_echo=case.local_echo,
    )
    try:
        print(f"[{case.name}] waiting for policy readiness", flush=True)
        _wait_for_network_policy_ready(container, case.name)
        print(f"[{case.name}] policy ready", flush=True)
        if case.local_echo:
            _wait_for_local_echo(container)
        print(f"[{case.name}] running {len(case.probes)} probes", flush=True)
        records = [_run_probe(container, probe) for probe in case.probes]
        failures = _validate_records(case.probes, records)
        if failures:
            print(f"[{case.name}] failed", flush=True)
        else:
            print(f"[{case.name}] passed", flush=True)
        return failures
    finally:
        _stop_quietly(container)


def _format_case_failures(failures_by_case: dict[str, list[str]]) -> str:
    lines = ["network policy cases failed:"]
    for case_name in POLICY_CASE_NAMES:
        failures = failures_by_case.get(case_name)
        if not failures:
            continue
        lines.append(f"[{case_name}]")
        lines.extend("  - " + failure for failure in failures)
    return "\n".join(lines)


def _run_netshoot_container(
    prefix: str,
    *,
    network_policy: NetworkPolicy | None = None,
    local_echo: bool = False,
) -> Container:
    container = Container(
        image=NETSHOOT_IMAGE,
        name=f"{prefix}-{uuid.uuid4().hex[:8]}",
        compute_class=ComputeClass.HOST,
        network_policy=network_policy,
        entrypoint=["bash", "-lc", LOCAL_ECHO_ENTRYPOINT]
        if local_echo
        else ["sleep", "600"],
    )
    try:
        return container.run()
    except BaseException:
        if container.id is not None:
            _stop_quietly(container)
        raise


def _stop_quietly(container: Container) -> None:
    try:
        container.stop()
    except Exception:
        pass


def _wait_for_network_policy_ready(container: Container, case_name: str) -> None:
    deadline = time.monotonic() + 60
    last_stdout = ""
    last_stderr = ""
    while True:
        try:
            dns_result = container.exec(
                "dig",
                "+time=1",
                "+tries=1",
                "+short",
                "A",
                POLICY_READY_DOMAIN,
                timeout_secs=5,
            )
        except ContainerError as exc:
            last_stdout = ""
            last_stderr = str(exc)
        else:
            last_stdout = dns_result.stdout_text.strip()
            last_stderr = dns_result.stderr_text.strip()
            if dns_result.exit_code == 0 and last_stdout:
                break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "policy readiness DNS check did not resolve "
                + POLICY_READY_DOMAIN
                + ": stdout="
                + repr(last_stdout)
                + " stderr="
                + repr(last_stderr)
            )
        time.sleep(0.5)

    if case_name == "allowall":
        return

    deadline = time.monotonic() + 60
    last_exit_code: int | None = None
    last_status: str | None = None
    last_body = ""
    last_stderr = ""
    route_block_ready = case_name in ROUTE_BLOCK_READY_CASE_NAMES
    while True:
        try:
            http_result = container.exec(
                "curl",
                "-4",
                "-sS",
                "--connect-timeout",
                "1",
                "--max-time",
                "2",
                "-w",
                "\n%{http_code}",
                POLICY_READY_URL,
                timeout_secs=7,
            )
        except ContainerError as exc:
            last_exit_code = None
            last_status = None
            last_body = ""
            last_stderr = str(exc)
        else:
            body, separator, status = http_result.stdout_text.rpartition("\n")
            last_exit_code = http_result.exit_code
            last_status = status.strip() if separator else None
            last_body = body[:600]
            last_stderr = http_result.stderr_text.strip()
            if last_status == "403" or (
                route_block_ready
                and last_status == "000"
                and last_exit_code not in {0, 28}
            ):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "policy readiness HTTP check did not return "
                + ("a fast route block for " if route_block_ready else "403 for ")
                + POLICY_READY_URL
                + ": exit="
                + repr(last_exit_code)
                + " status="
                + repr(last_status)
                + " stderr="
                + repr(last_stderr)
                + " body="
                + repr(last_body)
            )
        time.sleep(0.5)


def _wait_for_local_echo(container: Container) -> None:
    result = container.exec(
        "bash",
        "-lc",
        "for i in {1..50}; do "
        "curl -4 -sS --connect-timeout 1 --max-time 1 "
        + _local_echo_url()
        + " >/dev/null && exit 0; "
        "sleep 0.1; "
        "done; exit 1",
        timeout_secs=10,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            "local echo server did not become reachable: " + result.stderr_text.strip()
        )


def _run_probe(container: Container, probe: DnsProbe | HttpProbe) -> ProbeRecord:
    match probe:
        case DnsProbe():
            result = container.exec(
                "dig",
                "+time=1",
                "+tries=1",
                "+short",
                "A",
                probe.domain,
                timeout_secs=5,
            )
            return ProbeRecord(
                name=probe.name,
                expected_ok=probe.expected_ok,
                ok=result.exit_code == 0 and bool(result.stdout_text.strip()),
                exit_code=result.exit_code,
                target=probe.domain,
                stderr=result.stderr_text,
                body_sample=result.stdout_text[:600],
            )
        case HttpProbe():
            return _run_http_probe(container, probe)


def _run_http_probe(container: Container, probe: HttpProbe) -> ProbeRecord:
    connect_timeout = HTTP_CONNECT_TIMEOUT
    max_time = HTTP_MAX_TIME
    if not probe.expected_ok:
        connect_timeout = DENY_HTTP_CONNECT_TIMEOUT
        max_time = DENY_HTTP_MAX_TIME

    command = [
        "curl",
        "-4",
        "-sS",
        "--connect-timeout",
        str(connect_timeout),
        "--max-time",
        str(max_time),
        "--write-out",
        "\n%{http_code}",
    ]
    if probe.pass_mode == "curl_fail":
        command.append("--fail")
    if probe.method == "HEAD":
        command.append("--head")
    else:
        command += ["--request", probe.method]
    if probe.connect_to:
        command += ["--connect-to", probe.connect_to]
    if probe.header:
        command += ["--header", probe.header]
    command.append(probe.url)

    result = container.exec(*command, timeout_secs=max_time + 5)
    body, separator, status = result.stdout_text.rpartition("\n")
    if not separator:
        body = ""
        status = result.stdout_text.strip() or None
    else:
        status = status.strip() or None
    ok = _http_probe_ok(probe, result.exit_code, status, body)
    return ProbeRecord(
        name=probe.name,
        expected_ok=probe.expected_ok,
        ok=ok,
        exit_code=result.exit_code,
        target=probe.url,
        status=status,
        stderr=result.stderr_text,
        body_sample=body[:600],
    )


def _http_probe_ok(
    probe: HttpProbe,
    exit_code: int,
    status: str | None,
    body: str,
) -> bool:
    if probe.pass_mode == "firewall":
        return exit_code == 0 and status not in {"000", "403"}
    if probe.pass_mode == "body_contains":
        return (
            exit_code == 0
            and status not in {"000", "403"}
            and probe.body_contains is not None
            and probe.body_contains in body
        )
    if probe.pass_mode == "curl_fail":
        return exit_code == 0
    raise ValueError(f"unknown pass mode: {probe.pass_mode}")


def _validate_records(
    probes: list[DnsProbe | HttpProbe],
    records: list[ProbeRecord],
) -> list[str]:
    failures = []
    records_by_name = {record.name: record for record in records}
    if len(records_by_name) != len(records):
        failures.append("duplicate probe result names")

    for probe in probes:
        record = records_by_name.get(probe.name)
        if record is None:
            failures.append(f"{probe.name}: missing result")
            continue
        if record.expected_ok is not probe.expected_ok:
            failures.append(
                f"{probe.name}: runner expected_ok={probe.expected_ok} record expected_ok={record.expected_ok}"
            )
        if not probe.expected_ok and record.exit_code == 28:
            failures.append(
                f"{probe.name}: timed out instead of failing fast target={record.target} status={record.status}"
            )
        if record.ok is not probe.expected_ok:
            failures.append(
                f"{probe.name}: ok={record.ok} expected={probe.expected_ok} "
                f"target={record.target} exit={record.exit_code} status={record.status} "
                f"stderr={record.stderr!r} body={record.body_sample!r}"
            )
    return failures


def _build_policy_case(
    name: str,
    targets: PolicyTargets,
) -> PolicyCase:
    if name == "allowall":
        return PolicyCase(
            name,
            NetworkPolicy(allowed_routes=[NetworkPolicy.Route("0.0.0.0/0")]),
            _standard_policy_probes(targets, allow_all=True),
        )
    if name == "denyall":
        return PolicyCase(name, NetworkPolicy(), _standard_policy_probes(targets))
    if name == "subnets-only":
        return PolicyCase(
            name,
            NetworkPolicy(
                allowed_routes=[
                    NetworkPolicy.Route(probe.cidr)
                    for probe in targets.allowed_cidr_probes
                ],
                denied_routes=[probe.cidr for probe in targets.blocked_cidr_probes],
            ),
            _standard_policy_probes(targets, allowed_categories={"allowed_cidr"}),
        )
    if name == "domains-only":
        return PolicyCase(
            name,
            NetworkPolicy(allowed_hosts=DOMAIN_ALLOW_PATTERNS),
            _standard_policy_probes(targets, allowed_categories={"allowed_domain"}),
        )
    if name == "domains-and-subnets":
        return PolicyCase(
            name,
            NetworkPolicy(
                allowed_routes=[
                    NetworkPolicy.Route(probe.cidr)
                    for probe in targets.allowed_cidr_probes
                ],
                denied_routes=[probe.cidr for probe in targets.blocked_cidr_probes],
                allowed_hosts=DOMAIN_ALLOW_PATTERNS,
            ),
            _standard_policy_probes(
                targets,
                allowed_categories={"allowed_domain", "allowed_cidr"},
            ),
        )
    if name == "juked-destination":
        return PolicyCase(
            name,
            NetworkPolicy(allowed_hosts=[JUKED_DESTINATION_DOMAIN]),
            _juked_destination_probes(targets.juked_destination_ip),
        )
    if name == "domain-fronting":
        return PolicyCase(
            name,
            NetworkPolicy(allowed_hosts=[DOMAIN_FRONTING_SNI]),
            _domain_fronting_probes(),
        )
    if name == "deny-route-over-allowed-route":
        return PolicyCase(
            name,
            NetworkPolicy(
                allowed_routes=[
                    NetworkPolicy.Route(probe.cidr)
                    for probe in targets.denied_allowed_cidr_probes
                ],
                denied_routes=[
                    probe.cidr for probe in targets.denied_allowed_cidr_probes
                ],
            ),
            _denied_allowed_cidr_probes(targets),
        )
    if name == "deny-route-over-allowed-host":
        probe = targets.denied_allowed_host_probe
        return PolicyCase(
            name,
            NetworkPolicy(denied_routes=[probe.cidr], allowed_hosts=[probe.host]),
            _cidr_http_probes("denied_allowed_host", "denied_allowed_host", 1, probe),
        )
    if name == "match-rules":
        return PolicyCase(
            name,
            _match_policy(),
            _match_probes(),
        )
    if name == "forward-rules":
        return PolicyCase(
            name,
            _forward_policy(),
            _forward_probes(targets.transform_connect_ip),
            local_echo=True,
        )
    if name == "transform-rules":
        return PolicyCase(
            name,
            _transform_policy(),
            _transform_probes(targets.transform_connect_ip),
            local_echo=True,
        )
    if name == "rule-combinations":
        return PolicyCase(
            name,
            _rule_combinations_policy(),
            _rule_combination_probes(targets.transform_connect_ip),
            local_echo=True,
        )
    raise ValueError(f"unknown policy case: {name}")


def _standard_policy_probes(
    targets: PolicyTargets,
    *,
    allowed_categories: set[str] | None = None,
    allow_all: bool = False,
) -> list[DnsProbe | HttpProbe]:
    allowed_categories = allowed_categories or set()
    probes: list[DnsProbe | HttpProbe] = []
    for idx, domain in enumerate(ALLOWED_DOMAINS, 1):
        probes.append(
            _dns_probe("dns_allowed_domain_" + str(idx), "allowed_domain", domain)
        )
        probes.append(
            _http_probe(
                "http_allowed_domain_" + str(idx),
                "http",
                "allowed_domain",
                _expects_ok("http", "allowed_domain", allowed_categories, allow_all),
                "http://" + domain + "/",
            )
        )
        probes.append(
            _http_probe(
                "https_allowed_domain_" + str(idx),
                "https",
                "allowed_domain",
                _expects_ok("https", "allowed_domain", allowed_categories, allow_all),
                "https://" + domain + "/",
            )
        )

    for idx, domain in enumerate(targets.blocked_domains, 1):
        probes.append(
            _dns_probe("dns_blocked_domain_" + str(idx), "blocked_domain", domain)
        )
        probes.append(
            _http_probe(
                "http_blocked_domain_" + str(idx),
                "http",
                "blocked_domain",
                _expects_ok("http", "blocked_domain", allowed_categories, allow_all),
                "http://" + domain + "/",
            )
        )
        probes.append(
            _http_probe(
                "https_blocked_domain_" + str(idx),
                "https",
                "blocked_domain",
                _expects_ok("https", "blocked_domain", allowed_categories, allow_all),
                "https://" + domain + "/",
            )
        )

    for idx, probe in enumerate(targets.allowed_cidr_probes, 1):
        probes += _cidr_http_probes(
            "allowed",
            "allowed_cidr",
            idx,
            probe,
            expected_ok=_expects_ok(
                "http", "allowed_cidr", allowed_categories, allow_all
            ),
        )
    for idx, probe in enumerate(targets.blocked_cidr_probes, 1):
        probes += _cidr_http_probes(
            "blocked",
            "blocked_cidr",
            idx,
            probe,
            expected_ok=_expects_ok(
                "http", "blocked_cidr", allowed_categories, allow_all
            ),
        )
    return probes


def _denied_allowed_cidr_probes(targets: PolicyTargets) -> list[HttpProbe]:
    probes = []
    for idx, probe in enumerate(targets.denied_allowed_cidr_probes, 1):
        probes += _cidr_http_probes(
            "denied_allowed",
            "denied_allowed_cidr",
            idx,
            probe,
        )
    return probes


def _dns_probe(name: str, category: str, domain: str) -> DnsProbe:
    return DnsProbe(name=name, category=category, expected_ok=True, domain=domain)


def _http_probe(
    name: str,
    kind: str,
    category: str,
    expected_ok: bool,
    url: str,
    *,
    method: str = "GET",
    header: str | None = None,
    connect_to: str | None = None,
    pass_mode: str = "firewall",
    body_contains: str | None = None,
) -> HttpProbe:
    return HttpProbe(
        name=name,
        kind=kind,
        category=category,
        expected_ok=expected_ok,
        url=url,
        method=method,
        header=header,
        connect_to=connect_to,
        pass_mode=pass_mode,
        body_contains=body_contains,
    )


def _expects_ok(
    kind: str,
    category: str,
    allowed_categories: set[str],
    allow_all: bool,
) -> bool:
    return allow_all or kind == "dns" or category in allowed_categories


def _cidr_http_probes(
    name_part: str,
    category: str,
    idx: int,
    probe: CidrProbe,
    *,
    expected_ok: bool = False,
) -> list[HttpProbe]:
    host = probe.host
    target = probe.target
    return [
        _http_probe(
            "http_" + name_part + "_cidr_" + str(idx),
            "http",
            category,
            expected_ok,
            "http://" + host + "/",
            connect_to=host + ":80:" + target + ":80",
        ),
        _http_probe(
            "https_" + name_part + "_cidr_" + str(idx),
            "https",
            category,
            expected_ok,
            "https://" + host + "/",
            connect_to=host + ":443:" + target + ":443",
        ),
    ]


def _juked_destination_probes(juked_destination_ip: str) -> list[HttpProbe]:
    return [
        _http_probe(
            "http_juked_destination_1",
            "http",
            "allowed_domain",
            True,
            "http://" + JUKED_DESTINATION_DOMAIN + "/",
            connect_to=JUKED_DESTINATION_DOMAIN
            + ":80:"
            + juked_destination_ip
            + ":80",
        ),
        _http_probe(
            "https_juked_destination_1",
            "https",
            "allowed_domain",
            True,
            "https://" + JUKED_DESTINATION_DOMAIN + "/",
            connect_to=JUKED_DESTINATION_DOMAIN
            + ":443:"
            + juked_destination_ip
            + ":443",
        ),
    ]


def _domain_fronting_probes() -> list[HttpProbe]:
    return [
        _http_probe(
            "http_domain_fronting_blocked_host",
            "http",
            "blocked_domain_fronting",
            False,
            "http://" + DOMAIN_FRONTING_SNI + "/",
            header="Host: " + DOMAIN_FRONTING_BLOCKED_HOST,
        ),
        _http_probe(
            "https_domain_fronting_blocked_host",
            "https",
            "blocked_domain_fronting",
            False,
            "https://" + DOMAIN_FRONTING_SNI + "/",
            header="Host: " + DOMAIN_FRONTING_BLOCKED_HOST,
        ),
    ]


def _match_policy() -> NetworkPolicy:
    matcher = NetworkPolicy.Matcher
    key_value = NetworkPolicy.KeyValueMatcher
    match = NetworkPolicy.Match
    rule = NetworkPolicy.Rule
    return NetworkPolicy(
        allowed_hosts={
            MATCH_DOMAIN: [
                rule(match=match(path=matcher(exact="/chalk-match/path-exact"))),
                rule(
                    match=match(path=matcher(starts_with="/chalk-match/path-prefix/"))
                ),
                rule(
                    match=match(path=matcher(regex="^/chalk-match/path-regex/[0-9]+$"))
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/method"),
                        methods=["HEAD"],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/query-present"),
                        query_strings=[key_value(key=matcher(exact="present"))],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/query-value-exact"),
                        query_strings=[
                            key_value(
                                key=matcher(exact="mode"),
                                value=matcher(exact="exact"),
                            )
                        ],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/query-value-prefix"),
                        query_strings=[
                            key_value(
                                key=matcher(exact="mode"),
                                value=matcher(starts_with="prefix-"),
                            )
                        ],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/query-value-regex"),
                        query_strings=[
                            key_value(
                                key=matcher(exact="mode"),
                                value=matcher(regex="^token-[0-9]+$"),
                            )
                        ],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/header-present"),
                        headers=[key_value(key=matcher(exact="x-chalk-present"))],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/header-value-exact"),
                        headers=[
                            key_value(
                                key=matcher(exact="x-chalk-mode"),
                                value=matcher(exact="exact"),
                            )
                        ],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/header-value-prefix"),
                        headers=[
                            key_value(
                                key=matcher(exact="x-chalk-mode"),
                                value=matcher(starts_with="prefix-"),
                            )
                        ],
                    )
                ),
                rule(
                    match=match(
                        path=matcher(exact="/chalk-match/header-value-regex"),
                        headers=[
                            key_value(
                                key=matcher(exact="x-chalk-mode"),
                                value=matcher(regex="^token-[0-9]+$"),
                            )
                        ],
                    )
                ),
            ]
        }
    )


def _match_probes() -> list[HttpProbe]:
    probes = []
    for scheme in ("http", "https"):
        base = scheme + "://" + MATCH_DOMAIN
        prefix = scheme + "_"
        probes += [
            _http_probe(
                prefix + "match_path_exact_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/path-exact",
            ),
            _http_probe(
                prefix + "match_path_exact_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/path-exact/extra",
            ),
            _http_probe(
                prefix + "match_path_prefix_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/path-prefix/child",
            ),
            _http_probe(
                prefix + "match_path_prefix_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/path-prefixed",
            ),
            _http_probe(
                prefix + "match_path_regex_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/path-regex/123",
            ),
            _http_probe(
                prefix + "match_path_regex_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/path-regex/abc",
            ),
            _http_probe(
                prefix + "match_method_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/method",
                method="HEAD",
            ),
            _http_probe(
                prefix + "match_method_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/method",
            ),
            _http_probe(
                prefix + "match_query_present_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/query-present?present=1",
            ),
            _http_probe(
                prefix + "match_query_present_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/query-present?missing=1",
            ),
            _http_probe(
                prefix + "match_query_value_exact_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/query-value-exact?mode=exact",
            ),
            _http_probe(
                prefix + "match_query_value_exact_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/query-value-exact?mode=wrong",
            ),
            _http_probe(
                prefix + "match_query_value_prefix_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/query-value-prefix?mode=prefix-ok",
            ),
            _http_probe(
                prefix + "match_query_value_prefix_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/query-value-prefix?mode=wrong-prefix",
            ),
            _http_probe(
                prefix + "match_query_value_regex_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/query-value-regex?mode=token-123",
            ),
            _http_probe(
                prefix + "match_query_value_regex_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/query-value-regex?mode=token-abc",
            ),
            _http_probe(
                prefix + "match_header_present_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/header-present",
                header="x-chalk-present: 1",
            ),
            _http_probe(
                prefix + "match_header_present_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/header-present",
            ),
            _http_probe(
                prefix + "match_header_value_exact_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/header-value-exact",
                header="x-chalk-mode: exact",
            ),
            _http_probe(
                prefix + "match_header_value_exact_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/header-value-exact",
                header="x-chalk-mode: wrong",
            ),
            _http_probe(
                prefix + "match_header_value_prefix_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/header-value-prefix",
                header="x-chalk-mode: prefix-ok",
            ),
            _http_probe(
                prefix + "match_header_value_prefix_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/header-value-prefix",
                header="x-chalk-mode: wrong-prefix",
            ),
            _http_probe(
                prefix + "match_header_value_regex_allowed",
                scheme,
                "allowed_match",
                True,
                base + "/chalk-match/header-value-regex",
                header="x-chalk-mode: token-123",
            ),
            _http_probe(
                prefix + "match_header_value_regex_blocked",
                scheme,
                "blocked_match",
                False,
                base + "/chalk-match/header-value-regex",
                header="x-chalk-mode: token-abc",
            ),
        ]
    return probes


def _transform_policy() -> NetworkPolicy:
    matcher = NetworkPolicy.Matcher
    return NetworkPolicy(
        allowed_hosts={
            TRANSFORM_DOMAIN: [
                NetworkPolicy.Rule(
                    match=NetworkPolicy.Match(
                        path=matcher(exact="/chalk-transform/add")
                    ),
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-chalk-transform": TRANSFORM_HEADER_VALUE}
                        )
                    ],
                    forward_url=_local_echo_url(),
                ),
                NetworkPolicy.Rule(
                    match=NetworkPolicy.Match(
                        path=matcher(exact="/chalk-transform/overwrite")
                    ),
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-chalk-overwrite": TRANSFORM_OVERWRITE_VALUE}
                        )
                    ],
                    forward_url=_local_echo_url(),
                ),
            ]
        }
    )


def _transform_probes(transform_connect_ip: str) -> list[HttpProbe]:
    probes = []
    for scheme in ("http", "https"):
        base = scheme + "://" + TRANSFORM_DOMAIN
        prefix = scheme + "_"
        probes += [
            _http_probe(
                prefix + "transform_header_added",
                scheme,
                "transformed",
                True,
                base + "/chalk-transform/add",
                connect_to=_domain_connect_to(
                    TRANSFORM_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-transform: " + TRANSFORM_HEADER_VALUE,
            ),
            _http_probe(
                prefix + "transform_header_overwritten",
                scheme,
                "transformed",
                True,
                base + "/chalk-transform/overwrite",
                header="x-chalk-overwrite: client-value",
                connect_to=_domain_connect_to(
                    TRANSFORM_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-overwrite: " + TRANSFORM_OVERWRITE_VALUE,
            ),
        ]
    return probes


def _forward_policy() -> NetworkPolicy:
    return NetworkPolicy(
        allowed_hosts={
            FORWARD_DOMAIN: [
                NetworkPolicy.Rule(forward_url=_local_echo_url()),
            ]
        }
    )


def _forward_probes(transform_connect_ip: str) -> list[HttpProbe]:
    probes = []
    for scheme in ("http", "https"):
        probes.append(
            _http_probe(
                scheme + "_forwarded_to_local_echo",
                scheme,
                "forwarded",
                True,
                scheme + "://" + FORWARD_DOMAIN + "/chalk-forward",
                header="x-chalk-forward: " + FORWARD_HEADER_VALUE,
                connect_to=_domain_connect_to(
                    FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-forward: " + FORWARD_HEADER_VALUE,
            )
        )
    return probes


def _rule_combinations_policy() -> NetworkPolicy:
    matcher = NetworkPolicy.Matcher
    return NetworkPolicy(
        allowed_hosts={
            TRANSFORM_DOMAIN: [
                NetworkPolicy.Rule(
                    match=NetworkPolicy.Match(
                        path=matcher(exact="/chalk-combo/match-transform")
                    ),
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-chalk-transform": TRANSFORM_HEADER_VALUE}
                        )
                    ],
                    forward_url=_local_echo_url(),
                ),
            ],
            MATCH_FORWARD_DOMAIN: [
                NetworkPolicy.Rule(
                    match=NetworkPolicy.Match(
                        path=matcher(exact="/chalk-combo/match-forward")
                    ),
                    forward_url=_local_echo_url(),
                ),
            ],
            TRANSFORM_FORWARD_DOMAIN: [
                NetworkPolicy.Rule(
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-chalk-transform": TRANSFORM_HEADER_VALUE}
                        )
                    ],
                    forward_url=_local_echo_url(),
                ),
            ],
            MATCH_TRANSFORM_FORWARD_DOMAIN: [
                NetworkPolicy.Rule(
                    match=NetworkPolicy.Match(path=matcher(exact="/chalk-combo/all")),
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-chalk-transform": TRANSFORM_HEADER_VALUE}
                        )
                    ],
                    forward_url=_local_echo_url(),
                ),
            ],
        }
    )


def _rule_combination_probes(transform_connect_ip: str) -> list[HttpProbe]:
    probes = [
        _http_probe(
            "http_combo_match_transform_allowed",
            "http",
            "combo_match_transform",
            True,
            "http://" + TRANSFORM_DOMAIN + "/chalk-combo/match-transform",
            connect_to=_domain_connect_to(
                TRANSFORM_DOMAIN,
                "http",
                transform_connect_ip,
            ),
            pass_mode="body_contains",
            body_contains="x-chalk-transform: " + TRANSFORM_HEADER_VALUE,
        ),
        _http_probe(
            "http_combo_match_transform_blocked",
            "http",
            "blocked_combo",
            False,
            "http://" + TRANSFORM_DOMAIN + "/chalk-combo/match-transform-blocked",
            connect_to=_domain_connect_to(
                TRANSFORM_DOMAIN,
                "http",
                transform_connect_ip,
            ),
        ),
    ]

    for scheme in ("http", "https"):
        probes += [
            _http_probe(
                scheme + "_combo_match_forward_allowed",
                scheme,
                "combo_match_forward",
                True,
                scheme + "://" + MATCH_FORWARD_DOMAIN + "/chalk-combo/match-forward",
                header="x-chalk-forward: " + FORWARD_HEADER_VALUE,
                connect_to=_domain_connect_to(
                    MATCH_FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-forward: " + FORWARD_HEADER_VALUE,
            ),
            _http_probe(
                scheme + "_combo_match_forward_blocked",
                scheme,
                "blocked_combo",
                False,
                scheme
                + "://"
                + MATCH_FORWARD_DOMAIN
                + "/chalk-combo/match-forward-blocked",
                connect_to=_domain_connect_to(
                    MATCH_FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
            ),
            _http_probe(
                scheme + "_combo_transform_forward",
                scheme,
                "combo_transform_forward",
                True,
                scheme
                + "://"
                + TRANSFORM_FORWARD_DOMAIN
                + "/chalk-combo/transform-forward",
                connect_to=_domain_connect_to(
                    TRANSFORM_FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-transform: " + TRANSFORM_HEADER_VALUE,
            ),
            _http_probe(
                scheme + "_combo_match_transform_forward_allowed",
                scheme,
                "combo_match_transform_forward",
                True,
                scheme + "://" + MATCH_TRANSFORM_FORWARD_DOMAIN + "/chalk-combo/all",
                connect_to=_domain_connect_to(
                    MATCH_TRANSFORM_FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
                pass_mode="body_contains",
                body_contains="x-chalk-transform: " + TRANSFORM_HEADER_VALUE,
            ),
            _http_probe(
                scheme + "_combo_match_transform_forward_blocked",
                scheme,
                "blocked_combo",
                False,
                scheme
                + "://"
                + MATCH_TRANSFORM_FORWARD_DOMAIN
                + "/chalk-combo/all-blocked",
                connect_to=_domain_connect_to(
                    MATCH_TRANSFORM_FORWARD_DOMAIN,
                    scheme,
                    transform_connect_ip,
                ),
            ),
        ]
    return probes


def _local_echo_url() -> str:
    return "http://127.0.0.1:" + str(LOCAL_ECHO_PORT) + "/headers"


def _domain_connect_to(domain: str, scheme: str, target: str) -> str:
    port = "443" if scheme == "https" else "80"
    return domain + ":" + port + ":" + target + ":" + port


def _resolve_cidr_probe(host: str) -> CidrProbe:
    target = _resolve_ipv4_candidates(host)[0]
    return CidrProbe(cidr=target + "/32", target=target, host=host)


def _resolve_ipv4_candidates(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(
            host,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError(
            f"could not resolve an IPv4 address for {host}: {exc}"
        ) from exc
    addresses = []
    seen = set()
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if addresses:
        return addresses
    raise RuntimeError("could not resolve an IPv4 address for " + host)
