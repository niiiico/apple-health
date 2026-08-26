"""Filesystem locations the pipeline needs defaults for."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Directory holding `data/` and `reports/`.

    `AH_REPO` wins when set. Otherwise this is derived from the source-tree
    layout, which is right for an editable checkout and wrong for a wheel —
    under a non-editable install the derivation lands in `site-packages`, so a
    container or `uv tool install` should set the variable. It is an override
    rather than a guess because writing session markdown into `site-packages`
    fails silently, which is the worst way for a path to be wrong.
    """
    override = os.environ.get("AH_REPO")
    return Path(override) if override else Path(__file__).resolve().parents[2]
