"""Hostile contract tests for RFC 002 source-adapter dispatch (#2062)."""

import contextlib
import os

import pytest

from mempalace import cli
from mempalace.sources import AdapterSchema, BaseSourceAdapter, DrawerRecord, SourceAdapterProtocolError, register, reset_adapters


class _Config:
    def __init__(self, palace_path=None):
        self.palace_path = palace_path or "/palace"
        self._file_config = {
            "api_key": "must-not-reach-adapters",
            "hall_keywords": {"work": ["project"]},
            "privacy_floor": None,
        }

    @property
    def hall_keywords(self):
        return self._file_config["hall_keywords"]

    @property
    def topic_wings(self):
        return ["work"]

    @property
    def privacy_floor(self):
        return self._file_config["privacy_floor"]

    @property
    def source_privacy_classes(self):
        return {}


class _KG:
    instances = []

    def __init__(self, db_path):
        self.db_path = db_path
        self.mutations = []
        self.closed = False
        self.__class__.instances.append(self)

    def add_triple(self, *args, **kwargs):
        self.mutations.append((args, kwargs))

    def close(self):
        self.closed = True


class _Collection:
    """Tiny stateful backend that fails on a requested candidate write."""

    def __init__(self):
        self.rows = {
            "old-id": {"document": "old durable content", "metadata": {"source_file": "item://1", "adapter_name": "replace", "version": "old"}}
        }
        self.upsert_calls = []
        self.delete_calls = []
        self.fail_next_upsert = False
        self.fail_on_upsert = None

    def get(self, *, ids=None, where=None, limit=None, include=None, **_kwargs):
        rows = list(self.rows.items())
        if ids is not None:
            rows = [(row_id, self.rows[row_id]) for row_id in ids if row_id in self.rows]
        elif where:
            def matches(meta, clause):
                if "$and" in clause:
                    return all(matches(meta, part) for part in clause["$and"])
                return all(meta.get(key) == value for key, value in clause.items())
            rows = [(row_id, row) for row_id, row in rows if matches(row["metadata"], where)]
        if limit is not None:
            rows = rows[:limit]
        return {
            "ids": [row_id for row_id, _ in rows],
            "documents": [row["document"] for _, row in rows],
            "metadatas": [row["metadata"] for _, row in rows],
        }

    def upsert(self, *, documents, ids, metadatas):
        self.upsert_calls.append((documents, ids, metadatas))
        if self.fail_next_upsert or self.fail_on_upsert == len(self.upsert_calls):
            self.fail_next_upsert = False
            raise OSError("simulated durable write failure")
        for document, row_id, metadata in zip(documents, ids, metadatas):
            self.rows[row_id] = {"document": document, "metadata": metadata}

    def delete(self, *, ids=None, where=None):
        self.delete_calls.append({"ids": ids, "where": where})
        if ids:
            for row_id in ids:
                self.rows.pop(row_id, None)

    def query(self, **_kwargs):
        return {}

    def count(self):
        return len(self.rows)


@pytest.fixture(autouse=True)
def _registry_isolation():
    reset_adapters()
    _KG.instances.clear()
    yield
    reset_adapters()


def _install(monkeypatch, collection):
    from mempalace import knowledge_graph, palace

    monkeypatch.setattr(cli, "MempalaceConfig", _Config)
    monkeypatch.setattr(palace, "get_collection", lambda _path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _KG)
    monkeypatch.setattr(palace, "mine_palace_lock", contextlib.nullcontext)


def test_replacement_write_failure_preserves_the_prior_durable_source_item(monkeypatch):
    class ReplaceAdapter(BaseSourceAdapter):
        name = "replace"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            yield DrawerRecord(content="new", source_file="item://1", metadata={"version": "new"})

        def describe_schema(self):
            from mempalace.sources import FieldSpec
            return AdapterSchema(version="1", fields={"version": FieldSpec(type="string", required=True, description="version")})

    collection = _Collection()
    collection.fail_next_upsert = True
    _install(monkeypatch, collection)
    register("replace", ReplaceAdapter)

    with pytest.raises(OSError, match="simulated durable write failure"):
        cli.mine_source_adapter(source_name="replace", source_path="/src", palace_path="/palace")

    assert collection.rows["old-id"]["document"] == "old durable content"
    assert all("old-id" not in (call["ids"] or []) for call in collection.delete_calls)


def test_partial_candidate_write_is_rolled_back_without_touching_prior_generation(monkeypatch):
    class ReplaceAdapter(BaseSourceAdapter):
        name = "replace"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            yield DrawerRecord(content="new-0", source_file="item://1", chunk_index=0)
            yield DrawerRecord(content="new-1", source_file="item://1", chunk_index=1)

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    collection = _Collection()
    collection.fail_on_upsert = 2
    _install(monkeypatch, collection)
    register("replace", ReplaceAdapter)

    with pytest.raises(OSError, match="simulated durable write failure"):
        cli.mine_source_adapter(source_name="replace", source_path="/src", palace_path="/palace")

    assert collection.rows == {
        "old-id": {
            "document": "old durable content",
            "metadata": {"source_file": "item://1", "adapter_name": "replace", "version": "old"},
        }
    }
    assert len(collection.delete_calls) == 1
    assert collection.delete_calls[0]["where"] is None
    assert "old-id" not in collection.delete_calls[0]["ids"]
    assert collection.upsert_calls[0][1][0] in collection.delete_calls[0]["ids"]


def test_adapter_gets_sanitized_config_and_read_only_kg_not_live_handles(monkeypatch):
    class HostileAdapter(BaseSourceAdapter):
        name = "hostile"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            assert not hasattr(palace.config, "_file_config")
            assert not hasattr(palace.config, "api_key")
            assert palace.config.hall_keywords == {"work": ("project",)}
            with pytest.raises((AttributeError, SourceAdapterProtocolError)):
                palace.knowledge_graph.add_triple("a", "b", "c")
            yield DrawerRecord(content="safe", source_file="item://safe")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    collection = _Collection()
    _install(monkeypatch, collection)
    register("hostile", HostileAdapter)

    assert cli.mine_source_adapter(source_name="hostile", source_path="/src", palace_path="/palace") == 1
    assert _KG.instances[0].mutations == []


def test_invalid_adapter_mode_or_transforms_fail_before_extraction_or_mutation(monkeypatch):
    class InvalidContractAdapter(BaseSourceAdapter):
        name = "invalid-contract"
        adapter_version = "1"
        supported_modes = frozenset({"not-a-mode"})
        declared_transformations = {"not-a-frozenset"}

        def ingest(self, *, source, palace):
            raise AssertionError("must not extract invalid adapter")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    collection = _Collection()
    _install(monkeypatch, collection)
    register("invalid-contract", InvalidContractAdapter)

    with pytest.raises(SourceAdapterProtocolError, match="supported_modes"):
        cli.mine_source_adapter(source_name="invalid-contract", source_path="/src", palace_path="/palace")
    assert collection.delete_calls == []
    assert collection.upsert_calls == []


def test_record_mode_must_be_supported_and_conflicting_mcp_daemon_modes_are_rejected(monkeypatch):
    class BadRecordModeAdapter(BaseSourceAdapter):
        name = "bad-record-mode"
        adapter_version = "1"
        supported_modes = frozenset({"whole_record"})

        def ingest(self, *, source, palace):
            yield DrawerRecord(content="bad", source_file="item://bad", metadata={"ingest_mode": "chunked_content"})

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    collection = _Collection()
    _install(monkeypatch, collection)
    register("bad-record-mode", BadRecordModeAdapter)
    with pytest.raises(SourceAdapterProtocolError, match="ingest_mode"):
        cli.mine_source_adapter(source_name="bad-record-mode", source_path="/src", palace_path="/palace")
    assert collection.delete_calls == []

    from mempalace import mcp_server, service
    monkeypatch.setattr(mcp_server, "_config", _Config())
    # run_mine owns this environment variable; ensure its mutation is restored
    # with the test rather than leaking into the MCP integration tests.
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", os.environ.get("MEMPALACE_PALACE_PATH", ""))
    assert mcp_server.tool_mine("source", mode="projects", source_adapter="bad-record-mode")["error_class"] == "ConflictingMineMode"
    result = service.run_mine({"source": "source", "source_adapter": "bad-record-mode", "mode": "projects", "palace_path": "/palace"})
    assert result["success"] is False
    assert result["error_class"] == "ConflictingMineMode"
