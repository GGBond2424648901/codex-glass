"""Paths and build identity shared by source and frozen runtimes."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


def resource_root() -> Path:
    """Return the project root in source runs or PyInstaller's bundle root."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Resolve a bundled resource without depending on the working directory."""
    return resource_root().joinpath(*parts)


def dashboard_build_identity() -> str:
    """Return one build identity for both the launcher and dashboard service."""
    path = (
        Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__).with_name("services") / "dashboard.py"
    )
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
