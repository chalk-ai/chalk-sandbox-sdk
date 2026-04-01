"""Tests for Image construction and integration with the sandbox service.

Unit tests (test_image_*) run without any network access.
Integration tests (test_create_*, test_list_*, test_build_*) use the Connect
protocol (HTTP+JSON) against the staging go-api-server.
"""

import os
import tempfile
import time

import httpx
import pytest
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict

from image import Image

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_CLIENT_SECRET = os.environ.get("CHALK_CLIENT_SECRET", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

SANDBOX_SERVICE_BASE = f"{CHALK_API_SERVER}/chalk.sandbox.v1.SandboxService"
CUSTOM_IMAGE_SERVICE_BASE = f"{CHALK_API_SERVER}/chalk.sandbox.v1.CustomImageService"

POLL_INTERVAL = 5
POLL_TIMEOUT = 600


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token via the Chalk REST API."""
    resp = httpx.post(
        f"{CHALK_API_SERVER}/v1/oauth/token",
        json={
            "client_id": CHALK_CLIENT_ID,
            "client_secret": CHALK_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    token = _get_access_token()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Chalk-Env-Id": CHALK_ENVIRONMENT_ID,
    }


def _sandbox_rpc(method: str, body: dict, headers: dict[str, str], timeout: float = 30) -> httpx.Response:
    """Make a Connect-protocol unary RPC call to SandboxService."""
    return httpx.post(
        f"{SANDBOX_SERVICE_BASE}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


def _custom_image_rpc(method: str, body: dict, headers: dict[str, str], timeout: float = 30) -> httpx.Response:
    """Make a Connect-protocol unary RPC call to CustomImageService."""
    return httpx.post(
        f"{CUSTOM_IMAGE_SERVICE_BASE}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


def _poll_until_ready(sandbox_id: str, headers: dict[str, str]) -> dict:
    """Poll GetSandbox until the sandbox reaches 'ready' or 'error' state."""
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        resp = _sandbox_rpc("GetSandbox", {"sandboxId": sandbox_id}, headers)
        assert resp.status_code == 200, f"GetSandbox failed: {resp.text}"
        data = resp.json()
        state = data.get("sandbox", {}).get("state", "")
        build_id = data.get("sandbox", {}).get("buildId", "")
        print(f"  poll: state={state} build_id={build_id}")
        if state == "ready":
            return data
        if state in ("error", "terminated"):
            pytest.fail(f"Sandbox {sandbox_id} entered {state} state: {data}")
    pytest.fail(f"Timed out after {POLL_TIMEOUT}s waiting for sandbox {sandbox_id}")


# ---------------------------------------------------------------------------
# Unit tests: Image construction
# ---------------------------------------------------------------------------


class TestImageBase:
    """Test Image.base() constructor."""

    def test_base_creates_image_with_given_ref(self) -> None:
        img = Image.base("ubuntu:22.04")
        spec = img.to_proto()
        assert spec.base_image == "ubuntu:22.04"
        assert len(spec.steps) == 0

    def test_base_arbitrary_registry(self) -> None:
        img = Image.base("ghcr.io/org/custom-image:v1.2.3")
        assert img.to_proto().base_image == "ghcr.io/org/custom-image:v1.2.3"


class TestImageDebianSlim:
    """Test Image.debian_slim() constructor."""

    def test_default_python_version(self) -> None:
        img = Image.debian_slim()
        assert img.to_proto().base_image == "python:3.12-slim-bookworm"

    def test_custom_python_version(self) -> None:
        img = Image.debian_slim("3.11")
        assert img.to_proto().base_image == "python:3.11-slim-bookworm"


class TestImageFromDockerfile:
    """Test Image.from_dockerfile() constructor."""

    def test_simple_dockerfile(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile", delete=False) as f:
            f.write("FROM python:3.12\nRUN pip install requests\nEXPOSE 8080\n")
            f.flush()
            img = Image.from_dockerfile(f.name)
        spec = img.to_proto()
        assert spec.base_image == "python:3.12"
        assert len(spec.steps) == 1
        assert spec.steps[0].dockerfile_commands.commands == [
            "RUN pip install requests",
            "EXPOSE 8080",
        ]

    def test_dockerfile_with_comments(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile", delete=False) as f:
            f.write("# Build stage\nFROM alpine:3.18\n# Install deps\nRUN apk add git\n")
            f.flush()
            img = Image.from_dockerfile(f.name)
        spec = img.to_proto()
        assert spec.base_image == "alpine:3.18"
        assert spec.steps[0].dockerfile_commands.commands == ["RUN apk add git"]

    def test_dockerfile_chaining(self) -> None:
        """from_dockerfile images can be further extended."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile", delete=False) as f:
            f.write("FROM python:3.12\nRUN echo base\n")
            f.flush()
            img = Image.from_dockerfile(f.name).pip_install(["numpy"])
        spec = img.to_proto()
        assert len(spec.steps) == 2
        assert spec.steps[1].pip_install.packages == ["numpy"]


class TestImageImmutability:
    """Verify builder methods return new instances without mutating the original."""

    def test_run_commands_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.run_commands("echo hello")
        assert len(base.to_proto().steps) == 0
        assert len(extended.to_proto().steps) == 1

    def test_pip_install_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.pip_install(["requests"])
        assert len(base.to_proto().steps) == 0
        assert len(extended.to_proto().steps) == 1

    def test_env_does_not_mutate(self) -> None:
        base = Image.debian_slim().env({"A": "1"})
        extended = base.env({"B": "2"})
        assert "B" not in base.to_proto().env
        assert extended.to_proto().env["A"] == "1"
        assert extended.to_proto().env["B"] == "2"

    def test_workdir_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.workdir("/app")
        assert not base.to_proto().HasField("workdir")
        assert extended.to_proto().workdir == "/app"

    def test_entrypoint_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.entrypoint(["/bin/bash"])
        assert base.to_proto().entrypoint == []
        assert extended.to_proto().entrypoint == ["/bin/bash"]

    def test_cmd_does_not_mutate(self) -> None:
        base = Image.debian_slim()
        extended = base.cmd(["--help"])
        assert base.to_proto().cmd == []
        assert extended.to_proto().cmd == ["--help"]


class TestImageRunCommands:
    """Test run_commands builder."""

    def test_single_command(self) -> None:
        img = Image.base("alpine").run_commands("apk add git")
        spec = img.to_proto()
        assert len(spec.steps) == 1
        assert spec.steps[0].run_commands.commands == ["apk add git"]

    def test_multiple_commands(self) -> None:
        img = Image.base("alpine").run_commands("apt-get update", "apt-get install -y git")
        spec = img.to_proto()
        assert spec.steps[0].run_commands.commands == [
            "apt-get update",
            "apt-get install -y git",
        ]

    def test_chained_run_commands(self) -> None:
        img = Image.base("alpine").run_commands("cmd1").run_commands("cmd2")
        spec = img.to_proto()
        assert len(spec.steps) == 2


class TestImagePipInstall:
    """Test pip_install builder."""

    def test_single_package(self) -> None:
        img = Image.debian_slim().pip_install(["requests"])
        assert img.to_proto().steps[0].pip_install.packages == ["requests"]

    def test_versioned_packages(self) -> None:
        img = Image.debian_slim().pip_install(["requests>=2.28", "pandas==2.0.0"])
        assert img.to_proto().steps[0].pip_install.packages == [
            "requests>=2.28",
            "pandas==2.0.0",
        ]

    def test_pip_install_from_requirements(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("requests>=2.28\n# comment\npandas\n\nnumpy\n")
            f.flush()
            img = Image.debian_slim().pip_install_from_requirements(f.name)
        assert img.to_proto().steps[0].pip_install.packages == [
            "requests>=2.28",
            "pandas",
            "numpy",
        ]


class TestImageAddFile:
    """Test add_local_file and add_local_dir builders."""

    def test_add_local_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("print('hello')")
            f.flush()
            img = Image.debian_slim().add_local_file(f.name, "/app/main.py")
        step = img.to_proto().steps[0].add_file
        assert step.destination == "/app/main.py"
        assert step.content == b"print('hello')"
        assert not step.HasField("mode")

    def test_add_local_file_with_mode(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/bash\necho hi")
            f.flush()
            img = Image.debian_slim().add_local_file(f.name, "/app/run.sh", mode=0o755)
        step = img.to_proto().steps[0].add_file
        assert step.mode == 0o755

    def test_add_local_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (p := __import__("pathlib").Path(d) / "a.py").write_text("a")
            (p2 := __import__("pathlib").Path(d) / "sub").mkdir()
            (p2 / "b.py").write_text("b")
            img = Image.debian_slim().add_local_dir(d, "/app")
        spec = img.to_proto()
        dests = [s.add_file.destination for s in spec.steps]
        assert "/app/a.py" in dests
        assert "/app/sub/b.py" in dests


class TestImageDockerfileCommands:
    """Test dockerfile_commands builder."""

    def test_raw_commands(self) -> None:
        img = Image.base("alpine").dockerfile_commands(["EXPOSE 8080", "LABEL maintainer=test"])
        step = img.to_proto().steps[0].dockerfile_commands
        assert step.commands == ["EXPOSE 8080", "LABEL maintainer=test"]


class TestImageEnvWorkdirEntrypointCmd:
    """Test env, workdir, entrypoint, and cmd builders."""

    def test_env_merges(self) -> None:
        img = Image.base("alpine").env({"A": "1"}).env({"B": "2", "A": "overridden"})
        env = dict(img.to_proto().env)
        assert env == {"A": "overridden", "B": "2"}

    def test_workdir(self) -> None:
        img = Image.base("alpine").workdir("/workspace")
        assert img.to_proto().workdir == "/workspace"

    def test_entrypoint(self) -> None:
        img = Image.base("alpine").entrypoint(["/bin/sh", "-c"])
        assert list(img.to_proto().entrypoint) == ["/bin/sh", "-c"]

    def test_cmd(self) -> None:
        img = Image.base("alpine").cmd(["--verbose", "--debug"])
        assert list(img.to_proto().cmd) == ["--verbose", "--debug"]


class TestImageRepr:
    """Test __repr__ for debugging."""

    def test_repr_minimal(self) -> None:
        r = repr(Image.base("alpine"))
        assert "alpine" in r

    def test_repr_with_steps(self) -> None:
        img = Image.debian_slim().pip_install(["requests"]).workdir("/app").env({"K": "V"})
        r = repr(img)
        assert "steps=1" in r
        assert "/app" in r
        assert "K" in r


class TestImageProtoSerialization:
    """Test proto serialization with all fields."""

    def test_full_image_serialization(self) -> None:
        img = (
            Image.debian_slim("3.12")
            .pip_install(["requests"])
            .run_commands("echo 'hello'")
            .workdir("/home/user")
            .env({"TEST_VAR": "hello"})
        )
        spec = img.to_proto()
        assert spec.base_image == "python:3.12-slim-bookworm"
        assert len(spec.steps) == 2
        assert spec.steps[0].pip_install.packages == ["requests"]
        assert spec.steps[1].run_commands.commands == ["echo 'hello'"]
        assert spec.workdir == "/home/user"
        assert spec.env["TEST_VAR"] == "hello"

    def test_json_serialization_for_connect(self) -> None:
        img = Image.debian_slim().pip_install(["requests"]).env({"K": "V"})
        spec_dict = MessageToDict(img.to_proto(), preserving_proto_field_name=True)
        assert spec_dict["base_image"] == "python:3.12-slim-bookworm"
        assert "steps" in spec_dict
        assert "env" in spec_dict

    def test_empty_image_serialization(self) -> None:
        """An image with no steps should serialize cleanly."""
        img = Image.base("scratch")
        spec = img.to_proto()
        assert spec.base_image == "scratch"
        assert len(spec.steps) == 0
        spec_dict = MessageToDict(spec, preserving_proto_field_name=True)
        assert spec_dict == {"base_image": "scratch"}


class TestImageChaining:
    """Test complex chaining patterns."""

    def test_shared_base_diverges(self) -> None:
        """Two images can share a base and diverge without interference."""
        base = Image.debian_slim().pip_install(["requests"])
        branch_a = base.pip_install(["pandas"])
        branch_b = base.run_commands("apt-get update")
        assert len(base.to_proto().steps) == 1
        assert len(branch_a.to_proto().steps) == 2
        assert len(branch_b.to_proto().steps) == 2
        assert branch_a.to_proto().steps[1].pip_install.packages == ["pandas"]
        assert branch_b.to_proto().steps[1].run_commands.commands == ["apt-get update"]

    def test_long_chain(self) -> None:
        img = (
            Image.debian_slim()
            .run_commands("apt-get update")
            .pip_install(["requests", "pandas"])
            .add_local_file(__file__, "/app/test.py")
            .dockerfile_commands(["EXPOSE 8080"])
            .env({"APP": "test"})
            .workdir("/app")
            .entrypoint(["python"])
            .cmd(["test.py"])
        )
        spec = img.to_proto()
        assert len(spec.steps) == 4
        assert spec.base_image == "python:3.12-slim-bookworm"
        assert spec.workdir == "/app"
        assert spec.env["APP"] == "test"
        assert list(spec.entrypoint) == ["python"]
        assert list(spec.cmd) == ["test.py"]


# ---------------------------------------------------------------------------
# Integration tests: require CHALK_* env vars
# ---------------------------------------------------------------------------

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@needs_staging
def test_list_sandboxes(auth_headers: dict[str, str]) -> None:
    """Verify we can authenticate and call ListSandboxes."""
    resp = _sandbox_rpc("ListSandboxes", {}, auth_headers)
    assert resp.status_code == 200, f"ListSandboxes failed: {resp.text}"
    data = resp.json()
    assert isinstance(data, dict)
    print(f"ListSandboxes response: {data}")


@needs_staging
def test_create_sandbox_with_debian_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a debian_slim ImageSpec using async polling."""
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'image built successfully'")
        .workdir("/home/user")
        .env({"TEST_VAR": "hello"})
    )

    spec_dict = MessageToDict(img.to_proto())
    body = {
        "imageSpec": spec_dict,
        "name": "test-debian-slim",
    }

    resp = _sandbox_rpc("CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]
    build_id = data["sandbox"].get("buildId")
    print(f"Created sandbox: {sandbox_id} state={state} build_id={build_id}")

    if state in ("building", "connecting"):
        print(f"Polling until ready (build_id={build_id})...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
        print(f"Sandbox {sandbox_id} is ready")
    else:
        assert state == "ready", f"Unexpected state: {state}"

    _sandbox_rpc("TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers)


@needs_staging
def test_create_sandbox_with_plain_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a plain image string and poll until ready."""
    body = {
        "image": "python:3.12-slim-bookworm",
        "name": "test-plain-image",
    }

    resp = _sandbox_rpc("CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox (plain) response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]

    if state in ("building", "connecting"):
        print("Polling until ready...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
    else:
        assert state == "ready", f"Unexpected state: {state}"

    print(f"Sandbox {sandbox_id} is ready")
    _sandbox_rpc("TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers)


@needs_staging
@pytest.mark.skip(reason="CustomImageService not yet deployed to staging — enable after go-api-server changes land")
def test_build_custom_image(auth_headers: dict[str, str]) -> None:
    """Build a custom image via CustomImageService.BuildCustomImage and query it via GetCustomImage."""
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'custom image built'")
    )

    spec_dict = MessageToDict(img.to_proto())
    body = {"imageSpec": spec_dict}

    resp = _custom_image_rpc("BuildCustomImage", body, auth_headers, timeout=600)
    assert resp.status_code == 200, f"BuildCustomImage failed: {resp.text}"

    data = resp.json()
    print(f"BuildCustomImage response: {data}")
    assert "image" in data
    assert "buildId" in data

    build_id = data["buildId"]

    # Query the build by ID
    get_resp = _custom_image_rpc("GetCustomImage", {"buildId": build_id}, auth_headers)
    assert get_resp.status_code == 200, f"GetCustomImage failed: {get_resp.text}"

    get_data = get_resp.json()
    print(f"GetCustomImage response: {get_data}")
    assert get_data["buildId"] == build_id
    assert get_data["status"] == "succeeded"
    assert get_data["image"] == data["image"]
