from __future__ import annotations

from pathlib import Path
from typing import Protocol

from chalkcompute._weights_format import WeightManifest


class EngineAdapter(Protocol):
    def launch_cmd(self, model_dir: Path, port: int) -> list[str]: ...

    async def apply(self, version_dir: Path, manifest: WeightManifest) -> None: ...

    async def reset_cache(self) -> None: ...

    async def ready(self) -> bool: ...
