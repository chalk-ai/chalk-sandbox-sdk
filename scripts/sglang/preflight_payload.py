"""CPU-only payload for a pinned SGLang image; imports no accelerator code on import.

The guarded one-shot guest invokes run_preflight and persists its returned proof.
This module never creates resources, downloads models, installs packages or
initializes CUDA. The caller owns the output directory and remote lifecycle.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import shutil
import tarfile
from importlib import metadata, util
from pathlib import Path

IMAGE = "lmsysorg/sglang@sha256:354700f4404ab0b8dcb493bc61d5d11e7c2a91f3a7babb4efe741cb75d4b51c0"
COMMIT = "297e54fd105e974cdf401e5b4d20253a22c16f90"
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
SOURCE_FILES = {
    "srt/entrypoints/http_server.py": "http_server.py",
    "srt/entrypoints/openai/protocol.py": "openai_protocol.py",
    "srt/managers/io_struct.py": "io_struct.py",
    "srt/managers/tokenizer_manager.py": "tokenizer_manager.py",
    "srt/managers/tokenizer_control_mixin.py": "tokenizer_control_mixin.py",
    "srt/model_executor/model_runner_components/weight_updater.py": "weight_updater.py",
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def verify_sources(package: Path, expected: dict[str, str]) -> dict[str, str]:
    if set(expected) != set(SOURCE_FILES):
        raise ValueError("Require every audited native control/loader source hash")
    actual = {}
    for relative in SOURCE_FILES:
        path = (package / relative).resolve()
        if not path.is_relative_to(package.resolve()):
            raise ValueError("SGLang source escapes package")
        actual[relative] = digest(path)
        if actual[relative] != expected[relative]:
            raise ValueError(f"SGLang source differs from audited commit: {relative}")
    return actual


def validate_versions(
    versions: dict[str, str], *, cuda: str | None, hip: str | None
) -> None:
    if versions["sglang"] != "0.5.23":
        raise ValueError("Require exactly SGLang 0.5.23")
    if versions["torch"].split("+", 1)[0] != "2.14.1":
        raise ValueError("Installed Torch differs from audited CUDA source pin2.14.1")
    if versions["transformers"] != "5.19.0":
        raise ValueError("Installed Transformers differs from audited source pin5.19.0")
    if cuda is None or cuda.split(".", 1)[0] != "13" or hip is not None:
        raise ValueError("Require the audited CUDA13 build; ROCm evidence is separate")


def verify_native_schemas(package: Path) -> dict:
    """Check audited request declarations without importing native/GPU modules.

    This is source schema evidence only. Native CLI help/import and actual
    request parsing remain unexecuted until the separately budgeted device run.
    """
    expected = {
        "srt/managers/io_struct.py": {
            "PauseGenerationReqInput": {
                "mode": ('Literal["abort", "retract", "in_place"]', '"abort"'),
            },
            "ContinueGenerationReqInput": {"torch_empty_cache": ("bool", "True")},
            "UpdateWeightFromDiskReqInput": {
                "model_path": ("str", None),
                "load_format": ("Optional[str]", "None"),
                "abort_all_requests": ("bool", "False"),
                "weight_version": ("Optional[str]", "None"),
                "is_async": ("bool", "False"),
                "torch_empty_cache": ("bool", "False"),
                "keep_pause": ("bool", "False"),
                "recapture_cuda_graph": ("bool", "False"),
                "token_step": ("int", "0"),
                "flush_cache": ("bool", "True"),
                "manifest": ("Optional[Dict[str, Any]]", "None"),
            },
        },
        "srt/entrypoints/openai/protocol.py": {
            "CompletionRequest": {"ignore_eos": ("bool", "False")},
        },
    }
    checked = {}
    for relative, classes in expected.items():
        tree = ast.parse((package / relative).read_text())
        declarations = {
            node.name: {
                field.target.id: field
                for field in node.body
                if isinstance(field, ast.AnnAssign)
                and isinstance(field.target, ast.Name)
            }
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        for name, fields in classes.items():
            for field_name, (annotation, default) in fields.items():
                field = declarations.get(name, {}).get(field_name)
                if (
                    field is None
                    or ast.dump(field.annotation)
                    != ast.dump(ast.parse(annotation).body[0].value)
                    or (ast.dump(field.value) if field.value is not None else None)
                    != (
                        ast.dump(ast.parse(default).body[0].value)
                        if default is not None
                        else None
                    )
                ):
                    raise ValueError(
                        f"Native request schema differs: {name}.{field_name}"
                    )
                checked[f"{name}.{field_name}"] = {
                    "annotation": ast.unparse(field.annotation),
                    "default": ast.unparse(field.value)
                    if field.value is not None
                    else None,
                }
    return {
        "method": "static_ast_without_native_imports",
        "fields": checked,
        "native_cli_help_executed": False,
        "native_request_parsing_executed": False,
    }


def bundle_fixture(root: Path, destination: Path) -> dict:
    if destination.exists() or destination.is_relative_to(root):
        raise ValueError("Bundle must be a new file outside its source tree")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Fixture requires ordinary local files")
    if sum(path.stat().st_size for path in files) > MAX_BUNDLE_BYTES:
        raise ValueError("Uncompressed fixture exceeds20MiB bound")
    with tarfile.open(destination, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(root), recursive=False)
    if destination.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Compressed fixture exceeds20MiB bound")
    return {
        "file": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": digest(destination),
    }


def validate_fixture_changes(changes: list[float]) -> None:
    if len(changes) != 2 or any(
        not math.isfinite(change) or change <= 0.01 for change in changes
    ):
        raise ValueError("Every fixture update must change CPU logits by >0.01")


def make_fixture(root: Path) -> dict:
    import torch
    from safetensors.torch import load_file, save_file
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast

    from chalkcompute._weights_codec import checksum, diff, encode_patch
    from chalkcompute._weights_format import (
        TensorEntry,
        WeightManifest,
        architecture_hash,
        pointer_json,
        version_dir,
    )
    from chalkcompute.weights.serving.full_checkpoint import materialize
    from chalkcompute.weights.serving.syncer import ApplyPlan

    root.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.manual_seed(20261105)
    config = LlamaConfig(
        vocab_size=128,
        hidden_size=256,
        intermediate_size=512,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=64,
        max_position_embeddings=8192,
        rms_norm_eps=1e-6,
        attention_bias=False,
        mlp_bias=False,
        tie_word_embeddings=False,
        bos_token_id=1,
        eos_token_id=2,
        pad_token_id=0,
    )
    config._attn_implementation = "eager"
    model = LlamaForCausalLM(config).to(dtype=torch.bfloat16)
    model.eval()
    initial = {
        name: tensor.detach().clone().contiguous()
        for name, tensor in model.state_dict().items()
    }
    assert sum(tensor.numel() for tensor in initial.values()) == 1_246_464
    assert all(
        tensor.dtype == torch.bfloat16 and torch.isfinite(tensor).all()
        for tensor in initial.values()
    )
    stream = root / "stream"
    keyframe = stream / version_dir(0)
    keyframe.mkdir(parents=True)
    model.save_pretrained(keyframe, safe_serialization=True)
    vocabulary = {
        "<pad>": 0,
        "<s>": 1,
        "</s>": 2,
        "<unk>": 3,
        **{f"v{index}": index for index in range(4, 128)},
    }
    native = Tokenizer(models.WordLevel(vocabulary, unk_token="<unk>"))
    native.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=native,
        pad_token="<pad>",
        bos_token="<s>",
        eos_token="</s>",
        unk_token="<unk>",
    )
    tokenizer.save_pretrained(keyframe)
    saved_config = json.loads((keyframe / "config.json").read_bytes())
    saved_config.update(
        architectures=["LlamaForCausalLM"],
        model_type="llama",
        torch_dtype="bfloat16",
        dtype="bfloat16",
    )
    (keyframe / "config.json").write_text(json.dumps(saved_config, sort_keys=True))
    architecture = architecture_hash(
        (keyframe / "config.json").read_bytes(), initial.items()
    )
    payload = (keyframe / "model.safetensors").read_bytes()
    entries = tuple(
        TensorEntry(
            name,
            "bfloat16",
            tuple(tensor.shape),
            "dense",
            "model.safetensors",
            0,
            len(payload),
            tensor.numel(),
            checksum(payload),
        )
        for name, tensor in initial.items()
    )
    manifests = [
        WeightManifest(
            0, None, "keyframe", "generated-tiny-llama", architecture, entries
        )
    ]
    previous = initial
    cpu_logits = []
    for version in range(3):
        state = {name: tensor.clone() for name, tensor in initial.items()}
        if version >= 1:
            state["lm_head.weight"][4].add_(0.5)
        if version == 2:
            state["model.layers.0.self_attn.q_proj.weight"][0].add_(0.1)
            state["model.norm.weight"].mul_(2)
        oracle = root / f"oracle-v{version}"
        oracle.mkdir()
        for asset in keyframe.iterdir():
            if asset.is_file() and asset.suffix != ".safetensors":
                shutil.copyfile(asset, oracle / asset.name)
        save_file(state, str(oracle / "model.safetensors"), metadata={"format": "pt"})
        # Independent model CPU logits calibrate a measurable fixture change;
        # served-vs-fresh device output remains the actual native oracle.
        model.load_state_dict(state, strict=True)
        with torch.no_grad():
            logits = (
                model(input_ids=torch.tensor([[4, 5, 6]], dtype=torch.long))
                .logits[0, -1]
                .float()
            )
        assert torch.isfinite(logits).all()
        cpu_logits.append(logits)
        if version:
            directory = stream / version_dir(version)
            directory.mkdir()
            entries = []
            with (directory / "patches.bin").open("wb") as writer:
                for name, tensor in state.items():
                    patch = diff(previous[name], tensor, name=name)
                    if patch is None:
                        continue
                    encoded = encode_patch(patch)
                    offset = writer.tell()
                    writer.write(encoded)
                    entries.append(
                        TensorEntry(
                            name,
                            patch.dtype,
                            patch.shape,
                            patch.encoding,
                            "patches.bin",
                            offset,
                            len(encoded),
                            patch.nnz,
                            checksum(encoded),
                        )
                    )
            manifests.append(
                WeightManifest(
                    version,
                    version - 1,
                    "patch",
                    "generated-tiny-llama",
                    architecture,
                    tuple(entries),
                )
            )
        previous = state
    for manifest in manifests:
        (stream / version_dir(manifest.version) / "manifest.json").write_bytes(
            manifest.to_json()
        )
        materialized = root / f"materialized-v{manifest.version}"
        materialized.mkdir()
        materialize(
            stream,
            ApplyPlan(manifest.version, tuple(manifests[: manifest.version + 1])),
            materialized,
        )
        actual = load_file(str(materialized / "model.safetensors"))
        expected = load_file(
            str(root / f"oracle-v{manifest.version}" / "model.safetensors")
        )
        assert actual.keys() == expected.keys()
        assert all(
            torch.equal(
                actual[name].reshape(-1).view(torch.uint8),
                expected[name].reshape(-1).view(torch.uint8),
            )
            for name in actual
        )
        shutil.rmtree(materialized)
    (stream / "CURRENT").write_bytes(pointer_json(0))
    changes = [
        float((cpu_logits[index] - cpu_logits[index - 1]).abs().max())
        for index in (1, 2)
    ]
    validate_fixture_changes(changes)
    return {
        "parameters": 1_246_464,
        "seed": 20261105,
        "architecture_hash": architecture,
        "versions": [0, 1, 2],
        "all_materialized_tensor_bytes_equal": True,
        "cpu_max_logit_changes": changes,
        "files": {
            str(path.relative_to(root)): {
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        },
    }


def run_preflight(root: str | Path, source_hashes: dict[str, str]) -> dict:
    import platform
    import torch

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if torch.cuda.is_initialized():
        raise ValueError("CPU preflight must never initialize CUDA")
    packages = (
        "sglang",
        "torch",
        "transformers",
        "tokenizers",
        "triton",
        "flashinfer-python",
        "sglang-kernel",
        "safetensors",
        "aiohttp",
        "zstandard",
        "xxhash",
    )
    versions = {package: metadata.version(package) for package in packages}
    validate_versions(versions, cuda=torch.version.cuda, hip=torch.version.hip)
    spec = util.find_spec("sglang")
    if spec is None or spec.origin is None:
        raise ValueError("Cannot locate installed SGLang source")
    package = Path(spec.origin).parent
    source = verify_sources(package, source_hashes)
    schemas = verify_native_schemas(package)
    from chalkcompute.weights.serving.adapters.sglang import SGLangAdapter

    fixture = make_fixture(root / "fixture")
    # Validate the complete managed command/env contract without spawning native.
    command = SGLangAdapter(
        engine_args=["--context-length", "8192", "--mem-fraction-static", "0.5"]
    ).launch_cmd(root / "fixture" / "oracle-v0", 8001)
    bundle = bundle_fixture(root / "fixture", root / "fixture.tar.gz")
    if torch.cuda.is_initialized():
        raise ValueError("CPU fixture unexpectedly initialized CUDA")
    return {
        "kind": "sglang_cpu_image_preflight",
        "image": IMAGE,
        "audited_commit": COMMIT,
        "python": platform.python_version(),
        "versions": versions,
        "torch_cuda": torch.version.cuda,
        "torch_hip": torch.version.hip,
        "cuda_initialized": False,
        "source_sha256": source,
        "native_request_schema": schemas,
        "managed_command": command,
        "fixture": fixture,
        "bundle": bundle,
    }
