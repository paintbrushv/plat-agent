"""§5.6 -- nightly live E2E against the real Project Essex data room.

Skipped by default; run with `pytest -m e2e_live` or via the nightly CI job.
Requires: ONEDRIVE_CLIENT_ID + Files.ReadWrite.All scope already authorized.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e_live


REQUIRED_ENV = ["ONEDRIVE_CLIENT_ID", "ONEDRIVE_TENANT_ID"]


@pytest.fixture
def project_essex_data_room(tmp_path: Path) -> Path:
    """Download the live Project Essex data room from OneDrive into tmp."""
    for var in REQUIRED_ENV:
        if not os.environ.get(var):
            pytest.skip(f"e2e_live requires {var}")
    # Lazy import — the multifamily_underwriting onedrive client is in
    # the sibling repo; rely on UNDERWRITING_ENGINE_PATH to put it on
    # sys.path before this fixture is invoked.
    import sys
    engine_path = os.environ.get("UNDERWRITING_ENGINE_PATH")
    if engine_path and engine_path not in sys.path:
        sys.path.insert(0, engine_path)
    from engine.onedrive.client import OneDriveClient  # type: ignore[import-not-found]
    client = OneDriveClient.from_env()
    target = tmp_path / "project_essex"
    target.mkdir()
    client.download_folder("Project Essex/Data Room", target)
    return target


def test_real_data_room_project_essex_produces_memo(
    project_essex_data_room: Path, tmp_path: Path,
) -> None:
    """Slow (~15 min). Validates Playwright + dispatch + LLMs against live data."""
    import json
    from plat_agent.lifecycle.runner import run_lifecycle

    project_root = tmp_path / "project_root"
    project_root.mkdir()

    result = run_lifecycle(
        project_essex_data_room,
        deal_slug="project_essex_e2e",
        project_root=project_root,
    )
    assert result.exit_code == 0
    run_dir = (project_root / "runs" / "deals" / "project_essex_e2e"
               / "outputs" / result.run_id)
    assert (run_dir / "memo" / "memo.md").exists()
    assert (run_dir / "memo" / "memo.pdf").exists()
    # Sanity: comp scraping returned something
    comps = json.loads((run_dir / "comps" / "comps.json").read_text())
    assert len(comps["comps"]) >= 1
