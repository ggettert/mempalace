"""Durable, per-source generation markers for RFC 002 reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _marker_path(palace_path: str, adapter_name: str, source_file: str) -> Path:
    identity = f"{adapter_name}\0{source_file}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return Path(palace_path) / ".mempalace" / "source-reconciliation" / f"{digest}.json"


def write_marker(palace_path: str, marker: dict[str, Any]) -> Path | None:
    """Atomically persist a prepared/committing protocol marker before I/O."""
    # Test/in-memory callers historically passed a synthetic palace path.
    # A real palace is created before a writable collection is opened; retain
    # that production guarantee while preserving the public in-memory adapter
    # harness as a non-crash-safe compatibility mode.
    if not Path(palace_path).is_dir():
        return None
    path = _marker_path(palace_path, marker["adapter_name"], marker["source_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(marker, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def remove_marker(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def recover_markers(palace_path: str, collection: Any, adapter_name: str) -> None:
    """Finish or roll back interrupted generations for one adapter idempotently."""
    directory = Path(palace_path) / ".mempalace" / "source-reconciliation"
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An atomic rename means this can only be a manually-corrupted
            # marker. Preserve it rather than guessing which generation wins.
            raise RuntimeError(f"invalid source reconciliation marker: {path}")
        if marker.get("adapter_name") != adapter_name:
            continue
        phase = marker.get("phase")
        if phase == "prepared":
            collection.delete(ids=list(marker.get("candidate_ids") or []))
        elif phase == "committing":
            collection.delete(ids=list(marker.get("old_ids") or []))
        else:
            raise RuntimeError(f"unknown source reconciliation phase {phase!r} in {path}")
        remove_marker(path)
