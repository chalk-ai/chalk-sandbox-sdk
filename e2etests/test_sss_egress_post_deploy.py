"""Live SSS HOST egress checks for GCP staging."""

import ipaddress
import socket
import uuid

from chalkcompute import (
    ComputeClass,
    ExecutionBackend,
    NetworkPolicy,
    Sandbox,
    SandboxClient,
)
from e2etests.conftest import needs_staging

NETSHOOT_IMAGE = "nicolaka/netshoot:latest"
INTERNAL_FIXTURE = "cidr-allowed.staging.internal.gcp.chalk.dev"


@needs_staging
def test_sss_host_direct_egress() -> None:
    fixture_ip = socket.gethostbyname(INTERNAL_FIXTURE)
    assert not ipaddress.ip_address(fixture_ip).is_global, fixture_ip

    client = SandboxClient.from_env()
    sandbox = Sandbox(
        image=NETSHOOT_IMAGE,
        name=f"sss-egress-direct-{uuid.uuid4().hex[:8]}",
        compute_class=ComputeClass.HOST,
        execution_target=ExecutionBackend.SERVERLESS,
        network_policy=NetworkPolicy(allowed_routes=[NetworkPolicy.Route("0.0.0.0/0")]),
    )
    try:
        client.run(sandbox, timeout=300)
        for scheme, port in (("http", 80), ("https", 443)):
            result = sandbox.exec(
                "curl",
                "-4",
                "-ksS",
                "--connect-timeout",
                "3",
                "--max-time",
                "6",
                "--connect-to",
                f"{INTERNAL_FIXTURE}:{port}:{fixture_ip}:{port}",
                f"{scheme}://{INTERNAL_FIXTURE}/status/200",
                timeout_secs=12,
            )
            assert result.exit_code not in (0, 28), (
                scheme,
                result.stdout_text,
                result.stderr_text,
            )

        public = sandbox.exec(
            "curl",
            "-4",
            "-fsS",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "https://www.example.com/",
            timeout_secs=25,
        )
        assert public.exit_code == 0, public.stderr_text
    finally:
        if sandbox.id is not None:
            sandbox.terminate()
        client.close()


@needs_staging
def test_sss_host_forwarding_to_internal_fixture() -> None:
    public_ip = socket.gethostbyname("www.example.com")
    assert ipaddress.ip_address(public_ip).is_global, public_ip

    client = SandboxClient.from_env()
    sandbox = Sandbox(
        image=NETSHOOT_IMAGE,
        name=f"sss-egress-forward-{uuid.uuid4().hex[:8]}",
        compute_class=ComputeClass.HOST,
        execution_target=ExecutionBackend.SERVERLESS,
        network_policy=NetworkPolicy(
            allowed_hosts={
                INTERNAL_FIXTURE: [],
                "example.org": [],
                "www.example.com": [
                    NetworkPolicy.Rule(
                        forward_url=f"http://{INTERNAL_FIXTURE}/status/200"
                    )
                ],
            }
        ),
    )
    control = Sandbox(
        image=NETSHOOT_IMAGE,
        name=f"sss-egress-control-{uuid.uuid4().hex[:8]}",
        compute_class=ComputeClass.HOST,
        execution_target=ExecutionBackend.ENVIRONMENT,
        network_policy=NetworkPolicy(
            allowed_hosts={
                INTERNAL_FIXTURE: [],
                "www.example.com": [
                    NetworkPolicy.Rule(
                        forward_url=f"http://{INTERNAL_FIXTURE}/status/200"
                    )
                ],
            }
        ),
    )
    try:
        client.run(control, timeout=300)
        client.run(sandbox, timeout=300)
        for url, connect_to in (
            (
                f"http://{INTERNAL_FIXTURE}/status/200",
                f"{INTERNAL_FIXTURE}:80:{public_ip}:80",
            ),
            ("http://www.example.com/status/200", None),
        ):
            command = [
                "curl",
                "-4",
                "-sS",
                "--connect-timeout",
                "3",
                "--max-time",
                "6",
                "--write-out",
                "\n%{http_code}",
            ]
            if connect_to is not None:
                command.extend(("--connect-to", connect_to))
            # The environment's hosted pool reaches the same upstream through
            # the same proxy path without the serverless destination exclusions.
            healthy = control.exec(*command, url, timeout_secs=12)
            assert healthy.exit_code == 0 and healthy.stdout_text.endswith("\n200"), (
                url,
                healthy.stdout_text,
                healthy.stderr_text,
            )
            result = sandbox.exec(
                *command,
                url,
                timeout_secs=12,
            )
            assert result.exit_code != 28, (url, result.stderr_text)
            assert result.exit_code != 0 or not result.stdout_text.endswith("\n200"), (
                url,
                result.stdout_text,
                result.stderr_text,
            )

        public = sandbox.exec(
            "curl",
            "-4",
            "-fsS",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "https://example.org/",
            timeout_secs=25,
        )
        assert public.exit_code == 0, public.stderr_text
    finally:
        for instance in (sandbox, control):
            if instance.id is not None:
                instance.terminate()
        client.close()
