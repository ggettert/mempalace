"""Durable, per-source generation markers for RFC 002 reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


_MARKER_VERSION = 2


def _marker_path(palace_path: str, adapter_name: str, source_file: str) -> Path:
    identity = f"{adapter_name}\0{source_file}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return Path(palace_path) / ".mempalace" / "source-reconciliation" / f"{digest}.json"


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry change across power loss on POSIX filesystems."""
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        # The marker file itself is still fsynced.  Some non-POSIX / virtual
        # filesystems do not permit opening directories; do not pretend that
        # is a successful stronger guarantee.
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_marker(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid source reconciliation marker: {path}") from exc
    if not isinstance(marker, dict):
        raise RuntimeError(f"invalid source reconciliation marker: {path}")
    return marker


def _verify_marker_readback(path: Path, expected: dict[str, Any]) -> None:
    """Reject a torn/stale marker before it authorizes destructive retirement."""
    persisted = _load_marker(path)
    for field in ("adapter_name", "source_file", "generation", "phase", "candidate_ids"):
        if persisted.get(field) != expected.get(field):
            raise RuntimeError(
                f"source reconciliation marker readback mismatch for {field}: {path}"
            )


def write_marker(palace_path: str, marker: dict[str, Any]) -> Path | None:
    """Atomically persist and verify a reconciliation phase marker.

    The data file and its parent directory are fsynced: atomic rename alone
    does not make the new directory entry power-loss durable.  Readback also
    proves the complete candidate-ID set and generation are present before a
    caller may advance to drawer retirement.
    """
    # Test/in-memory callers historically passed a synthetic palace path.
    # A real palace is created before a writable collection is opened; retain
    # that production guarantee while preserving the public in-memory adapter
    # harness as a non-crash-safe compatibility mode.
    if not Path(palace_path).is_dir():
        return None
    path = _marker_path(palace_path, marker["adapter_name"], marker["source_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    _fsync_directory(path.parent)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(marker, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)
    _verify_marker_readback(path, marker)
    return path


def remove_marker(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def verify_candidate_generation(collection: Any, marker: dict[str, Any]) -> None:
    """Require every candidate ID to be durable in the announced generation."""
    candidate_ids = list(marker.get("candidate_ids") or [])
    if not candidate_ids:
        return
    result = collection.get(ids=candidate_ids)
    if not isinstance(result, dict):
        raise RuntimeError("candidate generation readback returned an invalid result")
    ids = result.get("ids") or []
    metadata = result.get("metadatas") or []
    by_id = {
        row_id: meta for row_id, meta in zip(ids, metadata)
        if isinstance(row_id, str) and isinstance(meta, dict)
    }
    missing = [row_id for row_id in candidate_ids if row_id not in by_id]
    wrong_generation = [
        row_id for row_id in candidate_ids
        if row_id in by_id and by_id[row_id].get("source_generation") != marker.get("generation")
    ]
    if missing or wrong_generation:
        raise RuntimeError(
            "candidate generation readback failed: "
            f"missing={missing!r}, wrong_generation={wrong_generation!r}"
        )


def repair_closets(marker: dict[str, Any], closets: Any) -> None:
    """Idempotently converge one adapter-scoped closet projection."""
    closet = marker.get("closet")
    if closets is None or not isinstance(closet, dict):
        return
    where = closet.get("where")
    if not isinstance(where, dict):
        raise RuntimeError("invalid source reconciliation closet repair marker")
    closets.delete(where=where)
    if closet.get("action") == "delete":
        return
    if closet.get("action") != "replace":
        raise RuntimeError("invalid source reconciliation closet repair action")
    for row in closet.get("rows") or []:
        if not isinstance(row, dict):
            raise RuntimeError("invalid source reconciliation closet row")
        row_id = row.get("id")
        document = row.get("document")
        metadata = row.get("metadata")
        if not isinstance(row_id, str) or not isinstance(document, str) or not isinstance(metadata, dict):
            raise RuntimeError("invalid source reconciliation closet row")
        closets.upsert(documents=[document], ids=[row_id], metadatas=[metadata])


def recover_markers(
    palace_path: str, collection: Any, adapter_name: str, *, closets: Any = None
) -> None:
    """Finish or roll back interrupted generations for one adapter idempotently.

    ``prepared`` rolls back an unverified candidate.  Every subsequent phase
    first retires exact old drawer IDs and then replays the marker's complete,
    adapter-scoped closet projection.  Thus a restart before a current/skip
    item is examined still converges the durable drawer/index state.
    """
    directory = Path(palace_path) / ".mempalace" / "source-reconciliation"
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        marker = _load_marker(path)
        if marker.get("adapter_name") != adapter_name:
            continue
        phase = marker.get("phase")
        if phase == "prepared":
            collection.delete(ids=list(marker.get("candidate_ids") or []))
        elif phase in {"retiring_drawers", "repairing_closets", "closets_repaired", "committing"}:
            # ``committing`` is the v1 spelling retained for markers created
            # by the previous implementation.  Those lack closet payloads,
            # so their drawer recovery remains safely backward compatible.
            collection.delete(ids=list(marker.get("old_ids") or []))
            repair_closets(marker, closets)
        else:
            raise RuntimeError(f"unknown source reconciliation phase {phase!r} in {path}")
        remove_marker(path)
