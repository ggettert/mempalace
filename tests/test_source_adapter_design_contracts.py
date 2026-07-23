"""Red/green design contracts for #2062's RFC 002 source reconciliation."""

import contextlib

import pytest

from mempalace import cli
from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    SourceAdapterProtocolError,
    register,
    reset_adapters,
)


class _Config:
    privacy = "pii_potential"

    def __init__(self, palace_path=None):
        self.palace_path = palace_path or "/palace"

    @property
    def hall_keywords(self):
        return {}

    @property
    def topic_wings(self):
        return []

    @property
    def privacy_floor(self):
        return None

    @property
    def source_privacy_classes(self):
        return {"privacy": self.__class__.privacy}


class _KG:
    def __init__(self, _path=None, **_kwargs):
        pass

    def close(self):
        pass


class _Collection:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.delete_interrupt = False

    def get(self, *, ids=None, where=None, limit=None, **_kwargs):
        pairs = list(self.rows.items())
        if ids is not None:
            pairs = [(row_id, self.rows[row_id]) for row_id in ids if row_id in self.rows]
        elif where:
            def match(meta, clause):
                if "$and" in clause:
                    return all(match(meta, child) for child in clause["$and"])
                return all(meta.get(key) == value for key, value in clause.items())

            pairs = [(row_id, row) for row_id, row in pairs if match(row["metadata"], where)]
        if limit is not None:
            pairs = pairs[:limit]
        return {
            "ids": [row_id for row_id, _ in pairs],
            "documents": [row["document"] for _, row in pairs],
            "metadatas": [row["metadata"] for _, row in pairs],
        }

    def upsert(self, *, documents, ids, metadatas):
        for document, row_id, metadata in zip(documents, ids, metadatas):
            self.rows[row_id] = {"document": document, "metadata": metadata}

    def delete(self, *, ids=None, where=None):
        if ids:
            for row_id in ids:
                self.rows.pop(row_id, None)
            if self.delete_interrupt:
                self.delete_interrupt = False
                raise KeyboardInterrupt("simulated process death after old-row deletion")
            return
        for row_id in self.get(where=where)["ids"]:
            self.rows.pop(row_id, None)

    def query(self, **_kwargs):
        return {}

    def count(self):
        return len(self.rows)


class _Closets(_Collection):
    pass


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_adapters()
    _Config.privacy = "pii_potential"
    yield
    reset_adapters()


def _install(monkeypatch, drawers, closets=None):
    from mempalace import knowledge_graph, palace

    monkeypatch.setattr(cli, "MempalaceConfig", _Config)
    monkeypatch.setattr(palace, "get_collection", lambda _path: drawers)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _KG)
    monkeypatch.setattr(palace, "mine_palace_lock", contextlib.nullcontext)
    monkeypatch.setattr(palace, "get_closets_collection", lambda _path: closets or _Closets())


def _rows_for(collection, adapter, source):
    return [
        row
        for row in collection.rows.values()
        if row["metadata"].get("adapter_name") == adapter
        and row["metadata"].get("source_file") == source
    ]


def test_crash_marker_recovers_partial_generation_with_a_fresh_invocation(monkeypatch, tmp_path):
    class Replace(BaseSourceAdapter):
        name = "replace"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            yield DrawerRecord("new", "item://1")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection({
        "old": {"document": "old", "metadata": {
            "adapter_name": "replace", "source_file": "item://1", "privacy_class": "pii_potential"
        }}
    })
    _install(monkeypatch, drawers)
    register("replace", Replace)
    drawers.delete_interrupt = True

    with pytest.raises(KeyboardInterrupt, match="process death"):
        cli.mine_source_adapter(source_name="replace", source_path="/src", palace_path=str(tmp_path))

    marker_dir = tmp_path / ".mempalace" / "source-reconciliation"
    assert list(marker_dir.glob("*.json")), "prepared/committing marker must survive process death"

    # A new invocation is the process-equivalent recovery boundary.
    cli.mine_source_adapter(source_name="replace", source_path="/src", palace_path=str(tmp_path))
    rows = _rows_for(drawers, "replace", "item://1")
    assert [row["document"] for row in rows] == ["new"]
    assert not list(marker_dir.glob("*.json"))


def test_privacy_downgrade_is_rejected_before_restart_extraction_or_writes(monkeypatch, tmp_path):
    class Privacy(BaseSourceAdapter):
        name = "privacy"
        adapter_version = "1"
        default_privacy_class = "sensitive"
        calls = 0

        def ingest(self, *, source, palace):
            self.__class__.calls += 1
            yield DrawerRecord("content", "item://privacy")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("privacy", Privacy)
    # First ingest is public, then the source policy raises its classification.
    _Config.privacy = "public"
    cli.mine_source_adapter(source_name="privacy", source_path="/src", palace_path=str(tmp_path))
    _Config.privacy = "sensitive"
    cli.mine_source_adapter(source_name="privacy", source_path="/src", palace_path=str(tmp_path))
    assert _rows_for(drawers, "privacy", "item://privacy")[0]["metadata"]["privacy_class"] == "sensitive"

    # A later downgrade has no migration/audit record and is rejected before
    # adapter execution or storage mutation.
    _Config.privacy = "public"
    Privacy.calls = 0
    with pytest.raises(SourceAdapterProtocolError, match="privacy downgrade"):
        cli.mine_source_adapter(source_name="privacy", source_path="/src", palace_path=str(tmp_path))
    assert Privacy.calls == 0
    assert _rows_for(drawers, "privacy", "item://privacy")[0]["metadata"]["privacy_class"] == "sensitive"


def test_unsupported_transform_is_rejected_before_adapter_extraction(monkeypatch, tmp_path):
    class BadTransform(BaseSourceAdapter):
        name = "bad-transform"
        adapter_version = "1"
        declared_transformations = frozenset({"not_a_registered_transform"})

        def ingest(self, *, source, palace):
            raise AssertionError("transform validation must run before extraction")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("bad-transform", BadTransform)
    with pytest.raises(SourceAdapterProtocolError, match="unsupported transformation"):
        cli.mine_source_adapter(source_name="bad-transform", source_path="/src", palace_path=str(tmp_path))
    assert drawers.rows == {}


def test_reserved_transform_is_accepted_before_adapter_extraction(monkeypatch, tmp_path):
    class SupportedTransform(BaseSourceAdapter):
        name = "supported-transform"
        adapter_version = "1"
        declared_transformations = frozenset({"newline_normalize"})

        def ingest(self, *, source, palace):
            yield DrawerRecord("content", "item://transform")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("supported-transform", SupportedTransform)
    assert cli.mine_source_adapter(
        source_name="supported-transform", source_path="/src", palace_path=str(tmp_path)
    ) == 1


def test_source_reconciliation_replaces_scoped_closets_and_tombstones_them(monkeypatch, tmp_path):
    class Index(BaseSourceAdapter):
        name = "index"
        adapter_version = "1"
        deleted = False

        def ingest(self, *, source, palace):
            if self.__class__.deleted:
                from mempalace.sources import SourceItemMetadata
                yield SourceItemMetadata("item://index", "__deleted__")
            else:
                yield DrawerRecord("fresh content", "item://index")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers, closets = _Collection(), _Closets({
        "other": {"document": "other", "metadata": {"adapter_name": "other", "source_file": "item://index"}},
        "stale": {"document": "stale", "metadata": {"adapter_name": "index", "source_file": "item://index"}},
    })
    _install(monkeypatch, drawers, closets)
    register("index", Index)
    cli.mine_source_adapter(source_name="index", source_path="/src", palace_path=str(tmp_path))
    assert [row["document"] for row in _rows_for(closets, "index", "item://index")] != ["stale"]
    assert _rows_for(closets, "other", "item://index")
    Index.deleted = True
    cli.mine_source_adapter(source_name="index", source_path="/src", palace_path=str(tmp_path))
    assert not _rows_for(closets, "index", "item://index")
    assert _rows_for(closets, "other", "item://index")


@pytest.mark.parametrize("via_helper", [False, True], ids=["yielded", "helper"])
def test_duplicate_logical_chunks_are_rejected_before_any_storage_mutation(monkeypatch, tmp_path, via_helper):
    class Duplicate(BaseSourceAdapter):
        name = "duplicate"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            record = DrawerRecord("same", "item://duplicate", chunk_index=0)
            if via_helper:
                palace.upsert_drawer(record)
                yield record
            else:
                yield record
                yield record

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("duplicate", Duplicate)
    with pytest.raises(SourceAdapterProtocolError, match="duplicate logical chunk"):
        cli.mine_source_adapter(source_name="duplicate", source_path="/src", palace_path=str(tmp_path))
    assert drawers.rows == {}


def test_adapter_cannot_reach_raw_storage_through_obvious_private_attributes(monkeypatch, tmp_path):
    class Hostile(BaseSourceAdapter):
        name = "hostile-private"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            assert not hasattr(palace, "_storage_collection")
            assert not hasattr(palace.drawer_collection, "_collection")
            assert "_storage_collection" not in vars(palace)
            yield DrawerRecord("safe", "item://safe")

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("hostile-private", Hostile)
    assert cli.mine_source_adapter(source_name="hostile-private", source_path="/src", palace_path=str(tmp_path)) == 1


def test_cli_mcp_and_daemon_apply_cli_wing_before_adapter_route(monkeypatch, tmp_path):
    class Routed(BaseSourceAdapter):
        name = "routed"
        adapter_version = "1"
        capabilities = frozenset({"adapter_owns_routing"})

        def ingest(self, *, source, palace):
            from mempalace.sources import RouteHint
            yield DrawerRecord("route", "item://route", route_hint=RouteHint(wing="adapter", room="adapter", hall="adapter"))

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("routed", Routed)
    cli.mine_source_adapter(
        source_name="routed", source_path="/src", palace_path=str(tmp_path), source_options={"wing": "cli"}
    )
    assert _rows_for(drawers, "routed", "item://route")[0]["metadata"]["wing"] == "cli"

    # MCP and daemon must delegate through the same source route policy.
    from mempalace import mcp_server, service
    monkeypatch.setattr(mcp_server, "_config", _Config(str(tmp_path)))
    monkeypatch.setattr(service, "MempalaceConfig", _Config)
    assert mcp_server.tool_mine("/src", source_adapter="routed", wing="mcp")["success"] is True
    assert service.run_mine({"source": "/src", "source_adapter": "routed", "wing": "daemon", "palace_path": str(tmp_path)})["success"] is True


def test_adapter_route_hint_requires_its_declared_capability(monkeypatch, tmp_path):
    class Unrouted(BaseSourceAdapter):
        name = "unrouted"
        adapter_version = "1"

        def ingest(self, *, source, palace):
            from mempalace.sources import RouteHint
            yield DrawerRecord("route", "item://route", route_hint=RouteHint(wing="untrusted"))

        def describe_schema(self):
            return AdapterSchema(version="1", fields={})

    drawers = _Collection()
    _install(monkeypatch, drawers)
    register("unrouted", Unrouted)
    with pytest.raises(SourceAdapterProtocolError, match="adapter_owns_routing"):
        cli.mine_source_adapter(source_name="unrouted", source_path="/src", palace_path=str(tmp_path))
    assert drawers.rows == {}
