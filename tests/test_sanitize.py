# sanitize gate — public release tree
"""Public-tree sanitize gate.

Fails if any private/company identity literal leaks into the public tree:
deal and property names, personal identity, private host paths, or the
company token. The gate file itself is skipped (it necessarily contains the
literals it forbids).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()

_FORBIDDEN = {
    # Personal identity
    "matthewdickson": "personal username",
    "matthew.edward.dickson": "personal email local part",
    "matthew dickson": "personal name",
    # Real deal slugs / property names
    "the_kress_building": "real deal slug",
    "kress": "real property name",
    "renew_at_fairmount": "real deal slug",
    "fairmount": "real property name",
    "woodford_on_mockingbird": "real deal slug",
    "woodford": "real property name",
    "anatole_on_briarwood": "real deal slug",
    "anatole": "real property name",
    "the_place_at_briarcrest": "real property name",
    "briarcrest": "real property name",
    "belle_mor": "real property name",
    "belle mor": "real property name",
    "belle-mor": "real property name",
    "university_cove": "real property name",
    "state_at_fishers": "real deal slug",
    "meridian_heights": "real property name",
    "forest hills": "real property name",
    "the_nolan": "real deal slug",
    "virtu_on_denali": "real deal slug",
    "west_oaks": "real property name",
    # Company / private host identity
    "upliftfunds": "company tenant hostname",
    "uplift partners": "company name",
    "sharepoint": "private SharePoint tenant references",
    # Host identity and private paths
    "/home/mdai": "private host path",
    "spark-17d5": "private hostname",
}

# Substring matches that are safe to require exactly (no word-boundary issues
# in ordinary English). "uplift" alone is word-boundary checked because
# "rent uplift" is legitimate domain vocabulary.
_WORD_FORBIDDEN = {
    "uplift": "company name (word-boundary)",
}

_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".pytest-tmp"}
_TEXT_SUFFIXES = {
    ".py", ".json", ".md", ".toml", ".txt", ".yml", ".yaml", ".cfg",
    ".ini", ".csv", ".html", ".css", ".js", ".sh", ".example", ".j2", "",
}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_private_literals_in_tree() -> None:
    hits: list[str] = []
    for path in _iter_text_files():
        if path.resolve() == _SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for snippet, why in _FORBIDDEN.items():
            if snippet in lowered:
                hits.append(f"{path.relative_to(REPO)}: {snippet!r} ({why})")
        for word, why in _WORD_FORBIDDEN.items():
            if re.search(r"\b" + re.escape(word) + r"\b", lowered):
                hits.append(f"{path.relative_to(REPO)}: {word!r} ({why})")
    assert hits == [], "private identity literals leaked:\n  " + "\n  ".join(hits)


def test_no_private_root_env_pins() -> None:
    """Hardcoded private data roots must not survive in shipped source."""
    hits: list[str] = []
    for path in _iter_text_files():
        if path.resolve() == _SELF:
            continue
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("/data/uplift", "/Users/", "spark-17d5"):
            if needle in text:
                hits.append(f"{path.relative_to(REPO)}: {needle!r}")
    assert hits == [], "private root/host pins in shipped source:\n  " + "\n  ".join(hits)