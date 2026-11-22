"""Bake a supplied SDK wheel into the local registry's function runtime image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from chalkcompute._local_dev import (
    BakedSDKRuntime,
    image_recipe_sha256,
    local_sdk_dependency_image,
)

_MANIFEST_MEDIA_TYPES = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
_RECIPE_VERSION = "1"


def standard_dependency_image(python_version: str) -> object:
    from chalkcompute import Image
    from chalkcompute._function import _remote_call_runtime_package_with_extras
    from chalkcompute._remote_function_tracing import (
        TracingPolicy,
        normalize_remote_function_tracing,
    )

    extras = (
        ["tracing"]
        if isinstance(normalize_remote_function_tracing(None), TracingPolicy)
        else []
    )
    return local_sdk_dependency_image(
        Image.debian_slim(python_version),
        _remote_call_runtime_package_with_extras(extras),
    )


def wheel_platform(wheel: Path) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", wheel.name):
        raise ValueError("Expected a Linux SDK wheel filename")
    parts = wheel.stem.rsplit("-", 3)
    if len(parts) != 4 or not parts[0].startswith("chalkcompute-"):
        raise ValueError("Expected a chalkcompute Linux wheel")
    python, abi, platform = parts[1:]
    if python != abi or not re.fullmatch(r"cp3\d+", python):
        raise ValueError(
            "Local SDK baking requires a CPython wheel with a matching ABI"
        )
    targets = set()
    for tag in platform.split("."):
        if tag.startswith(("linux_", "manylinux", "musllinux")) and tag.endswith(
            "_x86_64"
        ):
            targets.add("linux/amd64")
        elif tag.startswith(("linux_", "manylinux", "musllinux")) and tag.endswith(
            "_aarch64"
        ):
            targets.add("linux/arm64")
        else:
            raise ValueError(
                "Local SDK baking supports Linux x86_64 and aarch64 wheels only"
            )
    if len(targets) != 1:
        raise ValueError("Wheel platform tags must identify one architecture")
    with zipfile.ZipFile(wheel) as archive:
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/WHEEL")]
        if len(metadata) != 1:
            raise ValueError("Wheel requires one WHEEL metadata file")
        tags = {
            line.removeprefix("Tag:").strip()
            for line in archive.read(metadata[0]).decode().splitlines()
            if line.startswith("Tag:")
        }
        declared = {
            f"{py}-{abi_tag}-{platform_tag}"
            for tag in tags
            for py, abi_tag, platforms in [tag.split("-")]
            for platform_tag in platforms.split(".")
        }
        if not {f"{python}-{abi}-{tag}" for tag in platform.split(".")}.issubset(
            declared
        ):
            raise ValueError("Wheel filename tag does not match WHEEL metadata")
    return targets.pop()


def _registry_json(
    registry: str, repository: str, kind: str, reference: str
) -> tuple[str, dict]:
    request = urllib.request.Request(
        f"http://{registry}/v2/{repository}/{kind}/{reference}",
        headers={"Accept": _MANIFEST_MEDIA_TYPES},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if reference.startswith("sha256:") and digest != reference:
        raise ValueError("Local registry returned content with the wrong digest")
    return digest, json.loads(payload)


def _registry_image(registry: str, repository: str, reference: str) -> tuple[str, dict]:
    digest, manifest = _registry_json(registry, repository, "manifests", reference)
    if "manifests" in manifest:
        raise ValueError("Local SDK runtime requires a single-platform image manifest")
    _, config = _registry_json(
        registry, repository, "blobs", manifest["config"]["digest"]
    )
    return digest, config


def bake_runtime(
    wheel: Path, dependency_image: object, push_registry: str, pull_registry: str
) -> BakedSDKRuntime:
    from chalkcompute._image import Image

    assert isinstance(dependency_image, Image)
    platform = wheel_platform(wheel)
    with wheel.open("rb") as source:
        wheel_sha = hashlib.file_digest(source, "sha256").hexdigest()
    recipe_sha = image_recipe_sha256(dependency_image)
    if dependency_image.lazy_local_files or dependency_image._volume_mounts:
        raise ValueError("Bake a runtime dependency recipe without source volumes")
    # Base packages and recipe caching remain owned by Image.build(). Only the
    # supplied native wheel needs a local Docker context beyond its inline limit.
    # Match the stable handler environment that Functions and Classes add after
    # choosing their dependency recipe, so the existing resolved base is reused.
    base_uri = (
        dependency_image.env({"PYTHONPATH": "/app", "CHALK_REMOTE_FUNCTION": "1"})
        .build()
        .as_image_uri_if_possible()
    )
    if base_uri is None:
        raise ValueError("Image.build did not return a resolved runtime base")
    if not base_uri.startswith(pull_registry + "/"):
        raise ValueError(
            "Resolved function base must be in the configured local registry"
        )
    repository_ref = base_uri[len(pull_registry) + 1 :]
    repository, separator, reference = repository_ref.partition("@")
    if not separator:
        repository, reference = repository_ref.rsplit(":", 1)
    base_digest, base_config = _registry_image(push_registry, repository, reference)
    if f"{base_config['os']}/{base_config['architecture']}" != platform:
        raise ValueError(
            "SDK wheel architecture does not match the resolved function base"
        )
    identity = dict(
        wheel_sha256=wheel_sha,
        base_digest=base_digest,
        platform=platform,
        recipe_version=_RECIPE_VERSION,
    )
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    runtime_repository = "chalk/local-sdk-runtime"
    tag = "wheel-" + key
    labels = {"dev.chalk.local-sdk." + k: v for k, v in identity.items()}

    def cached_runtime() -> BakedSDKRuntime:
        digest, config = _registry_image(push_registry, runtime_repository, tag)
        if config.get("config", {}).get("Labels", {}) != labels:
            # Base images may carry unrelated labels. Check only our identity.
            actual = config.get("config", {}).get("Labels", {})
            if any(actual.get(k) != v for k, v in labels.items()):
                raise ValueError(
                    "Cached local SDK runtime metadata does not match its content key"
                )
        if f"{config['os']}/{config['architecture']}" != platform:
            raise ValueError(
                "Cached local SDK runtime architecture does not match the wheel"
            )
        return BakedSDKRuntime(
            f"{pull_registry}/{runtime_repository}@{digest}", wheel_sha, recipe_sha
        )

    try:
        return cached_runtime()
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    with tempfile.TemporaryDirectory(prefix="chalk-sdk-runtime-") as directory:
        context = Path(directory)
        target = context / wheel.name
        shutil.copyfile(wheel, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != wheel_sha:
            raise ValueError("SDK wheel changed while preparing its build context")
        dockerfile = (
            f"FROM {push_registry}/{repository}@{base_digest}\n"
            f"COPY {json.dumps([wheel.name, '/tmp/' + wheel.name])}\n"
            f"RUN python -m pip install --force-reinstall --no-deps /tmp/{wheel.name} && "
            'python -c "import json; from importlib.metadata import distribution; '
            "info=json.loads(distribution('chalkcompute').read_text('direct_url.json')); "
            f"assert info['archive_info']['hashes']['sha256'] == '{wheel_sha}'\" && rm /tmp/{wheel.name}\n"
            + "".join(f"LABEL {k}={json.dumps(v)}\n" for k, v in labels.items())
        )
        (context / "Dockerfile").write_text(dockerfile)
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                platform,
                "--provenance=false",
                "--sbom=false",
                "--tag",
                f"{push_registry}/{runtime_repository}:{tag}",
                "--push",
                str(context),
            ],
            check=True,
        )
    return cached_runtime()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--push-registry", default="localhost:5000")
    parser.add_argument("--pull-registry", default="k3d-chalk-registry.localhost:5000")
    options = parser.parse_args()
    wheel_platform(options.sdk_wheel)
    python_tag = options.sdk_wheel.stem.rsplit("-", 3)[1].removeprefix("cp")
    python_version = python_tag[0] + "." + python_tag[1:]
    recipe = standard_dependency_image(python_version)
    runtime = bake_runtime(
        options.sdk_wheel, recipe, options.push_registry, options.pull_registry
    )
    options.output.write_text(json.dumps(runtime.__dict__))


if __name__ == "__main__":
    main()
