"""Reviewed, installed package adapters for the public V3 integration.

The package digest is over the installed Python and packaged data files.  A
distribution's 0.1.0 label alone cannot distinguish the reviewed commit from
an older 0.1.0 build, so both the version and contents are checked before use.
Changing a producer requires a reviewed adapter version and a new digest.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PackageContractV1:
    distribution: str
    module: str
    version: str
    source_sha: str
    content_sha256: str
    suffixes: frozenset[str]
    file_count: int

    def verify(self) -> Path:
        try:
            installed_version = importlib.metadata.version(self.distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"{self.distribution} adapter v1 requires an installed package") from exc
        if installed_version != self.version:
            raise RuntimeError(
                f"{self.distribution} adapter v1 requires version {self.version}; "
                f"found {installed_version}"
            )
        spec = importlib.util.find_spec(self.module)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"{self.distribution} adapter v1 cannot locate {self.module}")
        root = Path(spec.origin).resolve().parent
        paths = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix in self.suffixes
            and "__pycache__" not in path.parts
        )
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        if len(paths) != self.file_count or digest.hexdigest() != self.content_sha256:
            raise RuntimeError(
                f"{self.distribution} adapter v1 package contents differ from "
                f"reviewed source {self.source_sha}; refusing calculations"
            )
        return root


COSTMODEL_V1 = PackageContractV1(
    distribution="plat-costmodel",
    module="plat_costmodel",
    version="0.1.0",
    source_sha="518142ecb8771e52fcc9985237fe1a6f97a76168",
    content_sha256="4e924264cec02bf8a74c89f42478f747d993fe7943b5041ee4720ef736933724",
    suffixes=frozenset({".py", ".yaml", ".sql"}),
    file_count=36,
)

UNDERWRITING_V1 = PackageContractV1(
    distribution="plat-multifamily-underwriting",
    module="engine",
    version="0.1.0",
    source_sha="0d106d601e6ae989d6942b424f8cd9b7b1173576",
    content_sha256="7523a609a441810c3a5102d50fa5fb96209580237bb9ae17f9bb94f995673783",
    suffixes=frozenset({".py", ".json"}),
    file_count=53,
)


UNDERWRITING_V2 = PackageContractV1(
    distribution="plat-multifamily-underwriting",
    module="engine",
    version="0.1.1",
    source_sha="10a88ed393e6d6611c8c710b5e15ef64e128af6b",
    content_sha256="06b73cd7a84a92ac3c7b35ad1eaca8ffd1bcc10291e23e8971cf190b95668e11",
    suffixes=frozenset({".py", ".json"}),
    file_count=54,
)

# The producer's first packaged MCP protocol ships in the V2 package.
UNDERWRITING_MCP_V1 = UNDERWRITING_V2
