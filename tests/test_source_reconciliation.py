"""Durability contracts for RFC 002 source reconciliation markers."""

import os

import pytest

from mempalace.sources.reconciliation import recover_markers, remove_marker, write_marker


def _marker(**overrides):
    marker = {
        "version": 2,
        "adapter_name": "adapter",
        "source_file": "item://source",
        "generation": "generation-123",
        "phase": "prepared",
        "old_ids": ["old"],
        "candidate_ids": ["candidate-a", "candidate-b"],
    }
    marker.update(overrides)
    return marker


def test_marker_replace_and_unlink_fsync_parent_and_read_back_candidate_generation(monkeypatch, tmp_path):
    fsynced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        fsynced.append(os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    path = write_marker(str(tmp_path), _marker())
    assert path is not None
    assert path.exists()
    remove_marker(path)
    assert not path.exists()
    # file fsync + parent directory fsync after replace and unlink
    assert len(fsynced) >= 3


def test_marker_readback_rejects_truncated_candidate_ids(monkeypatch, tmp_path):
    import mempalace.sources.reconciliation as reconciliation

    real_load = reconciliation._load_marker

    def truncated(path):
        marker = real_load(path)
        marker["candidate_ids"] = marker["candidate_ids"][:1]
        return marker

    monkeypatch.setattr(reconciliation, "_load_marker", truncated)
    with pytest.raises(RuntimeError, match="candidate_ids"):
        write_marker(str(tmp_path), _marker())


class _Rows:
    def __init__(self, rows):
        self.rows = dict(rows)

    def delete(self, *, ids=None, where=None):
        if ids:
            for row_id in ids:
                self.rows.pop(row_id, None)
            return
        for row_id, row in list(self.rows.items()):
            metadata = row["metadata"]
            clauses = where.get("$and", [where])
            if all(all(metadata.get(key) == value for key, value in clause.items()) for clause in clauses):
                self.rows.pop(row_id)

    def upsert(self, *, documents, ids, metadatas):
        for document, row_id, metadata in zip(documents, ids, metadatas):
            self.rows[row_id] = {"document": document, "metadata": metadata}


@pytest.mark.parametrize(
    "phase", ["prepared", "committing", "retiring_drawers", "repairing_closets", "closets_repaired"]
)
def test_recovery_converges_every_marker_phase_and_tombstone_closets(tmp_path, phase):
    drawers = _Rows({
        "old": {"document": "old", "metadata": {}},
        "candidate-a": {"document": "candidate", "metadata": {}},
        "candidate-b": {"document": "candidate", "metadata": {}},
    })
    closets = _Rows({
        "ours": {"document": "old closet", "metadata": {"adapter_name": "adapter", "source_file": "item://source"}},
        "other": {"document": "other closet", "metadata": {"adapter_name": "other", "source_file": "item://source"}},
    })
    marker = _marker(phase=phase, closet={
        "action": "delete",
        "where": {"$and": [{"adapter_name": "adapter"}, {"source_file": "item://source"}]},
        "rows": [],
    })
    write_marker(str(tmp_path), marker)
    recover_markers(str(tmp_path), drawers, "adapter", closets=closets)
    if phase == "prepared":
        assert "old" in drawers.rows
        assert "candidate-a" not in drawers.rows
        assert "ours" in closets.rows
    else:
        assert "old" not in drawers.rows
        assert "candidate-a" in drawers.rows
        assert "ours" not in closets.rows
    assert "other" in closets.rows
    assert not list((tmp_path / ".mempalace" / "source-reconciliation").glob("*.json"))
