"""Sparse-only serving policy: incompatible publications require replica restart."""

from __future__ import annotations

import hashlib

from chalkcompute._weights_format import WeightManifest, version_dir
from chalkcompute.weights.serving.resident import load_manifest, read_profile_file
from chalkcompute.weights.serving.syncer import ApplyPlan, StreamError, WeightSyncer


class RestartRequired(RuntimeError):
    """Restart from a keyframe; never send a dense reload to a resident adapter."""


class SparseWeightSyncer(WeightSyncer):
    def manifest(self, version: int) -> WeightManifest:
        directory = self.root / version_dir(version)
        raw = read_profile_file(directory, "manifest.json")
        manifest = load_manifest(directory, version, hashlib.sha256(raw).hexdigest())
        if (
            self.architecture_hash
            and manifest.architecture_hash != self.architecture_hash
        ):
            raise StreamError(
                "Model architecture changed; deploy a new serving replica"
            )
        return manifest

    def plan(self, target: int, *, bootstrap: bool = False) -> ApplyPlan:
        plan = super().plan(target, bootstrap=bootstrap)
        manifests = plan.manifests[1:] if bootstrap else plan.manifests
        if any(
            m.kind != "patch" or any(e.encoding != "sparse" for e in m.tensors)
            for m in manifests
        ):
            raise RestartRequired(
                "Sparse serving requires forward sparse patches; restart from a keyframe"
            )
        return plan
