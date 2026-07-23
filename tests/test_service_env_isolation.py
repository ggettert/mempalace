"""Service entrypoints must not leak per-job palace selection (#2062)."""

import os

from mempalace import service


def test_run_mine_restores_palace_env_even_for_a_validation_error(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)

    result = service.run_mine(
        {"palace_path": str(tmp_path), "source_adapter": "adapter", "mode": "projects"}
    )

    assert result["success"] is False
    assert "MEMPALACE_PALACE_PATH" not in os.environ
