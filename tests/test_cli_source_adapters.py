"""CLI coverage for explicit RFC 002 source-adapter dispatch (#2062)."""

import argparse
import contextlib
import os
import sys

import pytest

from mempalace import cli
from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    SchemaConformanceError,
    SourceAdapterProtocolError,
    SourceItemMetadata,
    RouteHint,
    PrivacyClassRejectedError,
    UnknownSourceAdapterError,
    get_adapter,
    register,
    reset_adapters,
    unregister,
)


class _FixtureAdapter(BaseSourceAdapter):
    name = "fixture"
    adapter_version = "0.1.0"
    instances = []

    def __init__(self):
        self.source = None
        self.palace = None
        self.__class__.instances.append(self)

    def ingest(self, *, source, palace):
        self.source = source
        self.palace = palace
        yield DrawerRecord(content="fixture content", source_file="fixture://record")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _FakeCollection:
    def __init__(self):
        self.upserts = []
        self.deletes = []
        self.gets = []
        self.existing_metadata = None

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)

    def get(self, **kwargs):
        self.gets.append(kwargs)
        return {"metadatas": [self.existing_metadata] if self.existing_metadata else []}

    def delete(self, **kwargs):
        self.deletes.append(kwargs)


class _FakeKnowledgeGraph:
    instances = []

    def __init__(self, db_path):
        self.db_path = db_path
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


class _FakeConfig:
    def __init__(self, palace_path=None):
        self.palace_path = palace_path or "/fake/palace"


@pytest.fixture(autouse=True)
def _isolated_fixture_adapter():
    _FixtureAdapter.instances.clear()
    _FakeKnowledgeGraph.instances.clear()
    reset_adapters()
    try:
        yield
    finally:
        unregister("fixture")
        reset_adapters()


def _mine_args(*, source=None, mode=None, dry_run=False):
    return argparse.Namespace(
        dir="/source",
        palace=None,
        source=source,
        mode=mode,
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=dry_run,
        no_gitignore=False,
        include_ignored=[],
        extract="exchange",
        daemon=False,
        background=False,
        max_chunks_per_file=None,
        redetect_origin=False,
        source_uri=False,
        source_option=[],
    )


def test_cmd_mine_source_dispatches_registered_adapter_through_palace_context(monkeypatch):
    from mempalace import knowledge_graph, palace

    collection = _FakeCollection()
    register("fixture", _FixtureAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "get_collection", lambda palace_path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)
    monkeypatch.setattr(palace, "mine_palace_lock", contextlib.nullcontext)

    cli.cmd_mine(_mine_args(source="fixture"))

    adapter = _FixtureAdapter.instances[0]
    assert adapter.source.local_path == "/source"
    assert adapter.palace.palace_path == "/fake/palace"
    assert adapter.palace.adapter_name == "fixture"
    assert adapter.palace.adapter_version == "0.1.0"
    # Adapters receive a constrained read-only collection facade; only core
    # retains the raw backend so they cannot bypass staged reconciliation.
    assert adapter.palace.drawer_collection is not collection
    assert adapter.palace.drawer_collection.get(where={}) == {"metadatas": []}
    assert adapter.palace.knowledge_graph is _FakeKnowledgeGraph.instances[0]
    assert _FakeKnowledgeGraph.instances[0].closed is True
    assert collection.upserts[0]["documents"] == ["fixture content"]
    assert collection.upserts[0]["metadatas"][0]["adapter_name"] == "fixture"


def test_cmd_mine_source_rejects_unknown_adapter(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_mine(_mine_args(source="not-installed"))

    assert excinfo.value.code == 2
    assert "unknown source adapter 'not-installed'" in capsys.readouterr().err


def test_cmd_mine_without_mode_preserves_projects_legacy_path(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    with patch("mempalace.miner.mine") as mine:
        cli.cmd_mine(_mine_args())

    mine.assert_called_once_with(
        project_dir="/source",
        palace_path="/fake/palace",
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        respect_gitignore=True,
        include_ignored=[],
        max_chunks_per_file=None,
    )


def test_mine_parser_rejects_explicit_source_and_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["mempalace", "mine", "/source", "--source", "fixture", "--mode", "projects"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


class _IncrementalAdapter(BaseSourceAdapter):
    name = "incremental"
    adapter_version = "0.1.0"

    def __init__(self):
        self.current_calls = []

    def ingest(self, *, source, palace):
        yield SourceItemMetadata(source_file="fixture://item", version="v2")
        yield DrawerRecord(
            content="new content",
            source_file="fixture://item",
            metadata={"version": "v2"},
        )

    def is_current(self, *, item, existing_metadata):
        self.current_calls.append((item, existing_metadata))
        return existing_metadata and existing_metadata.get("version") == item.version

    def describe_schema(self):
        return AdapterSchema(
            version="1.0",
            fields={
                "version": FieldSpec(type="string", required=True, description="source version")
            },
        )


def _install_normal_storage(monkeypatch, collection):
    from mempalace import knowledge_graph, palace

    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "get_collection", lambda _palace_path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)
    monkeypatch.setattr(palace, "mine_palace_lock", contextlib.nullcontext)


def test_adapter_protocol_uses_source_item_metadata_to_skip_current_drawers(monkeypatch):
    collection = _FakeCollection()
    collection.existing_metadata = {"version": "v2"}
    _install_normal_storage(monkeypatch, collection)
    register("incremental", _IncrementalAdapter)

    assert (
        cli.mine_source_adapter(
            source_name="incremental", source_path="/source", palace_path="/fake/palace"
        )
        == 0
    )
    adapter = get_adapter("incremental")
    assert adapter.current_calls[0][1] == {"version": "v2"}
    assert collection.upserts == []


def test_adapter_protocol_purges_tombstones_and_skips_following_drawers(monkeypatch):
    class TombstoneAdapter(BaseSourceAdapter):
        name = "tombstone"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(source_file="fixture://gone", version="__deleted__")
            yield DrawerRecord(content="must not write", source_file="fixture://gone")

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("tombstone", TombstoneAdapter)

    assert (
        cli.mine_source_adapter(
            source_name="tombstone", source_path="/source", palace_path="/fake/palace"
        )
        == 0
    )
    assert collection.deletes == [
        {
            "where": {
                "$and": [
                    {"source_file": "fixture://gone"},
                    {"adapter_name": "tombstone"},
                ]
            }
        }
    ]
    assert collection.upserts == []


def test_changed_source_item_is_replaced_and_incremental_lookup_is_adapter_scoped(monkeypatch):
    collection = _FakeCollection()
    collection.existing_metadata = {"version": "v1"}
    _install_normal_storage(monkeypatch, collection)
    register("incremental", _IncrementalAdapter)

    assert (
        cli.mine_source_adapter(
            source_name="incremental", source_path="relative-source", palace_path="/fake/palace"
        )
        == 1
    )

    source_file = "fixture://item"
    expected_where = {
        "$and": [
            {"source_file": source_file},
            {"adapter_name": "incremental"},
        ]
    }
    assert collection.gets == [{"where": expected_where, "limit": 1}]
    assert collection.deletes == [{"where": expected_where}]
    assert collection.upserts[0]["metadatas"][0]["source_file"] == source_file


def test_eager_adapter_replaces_stale_chunks_without_source_metadata(monkeypatch):
    class EagerAdapter(BaseSourceAdapter):
        name = "eager"

        def ingest(self, *, source, palace):
            yield DrawerRecord(content="only current chunk", source_file="fixture://item")

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("eager", EagerAdapter)

    assert (
        cli.mine_source_adapter(
            source_name="eager", source_path="/source", palace_path="/fake/palace"
        )
        == 1
    )
    assert collection.deletes == [
        {
            "where": {
                "$and": [
                    {"source_file": "fixture://item"},
                    {"adapter_name": "eager"},
                ]
            }
        }
    ]


@pytest.mark.parametrize(
    ("adapter_name", "adapter_cls", "error_type"),
    [
        (
            "bad-schema",
            type(
                "BadSchemaAdapter",
                (BaseSourceAdapter,),
                {
                    "name": "bad-schema",
                    "ingest": lambda self, **_kwargs: iter(
                        (DrawerRecord(content="x", source_file="fixture://bad", metadata={}),)
                    ),
                    "describe_schema": lambda self: AdapterSchema(
                        version="1.0",
                        fields={
                            "required": FieldSpec(type="string", required=True, description="x")
                        },
                    ),
                },
            ),
            SchemaConformanceError,
        ),
        (
            "bad-result",
            type(
                "BadResultAdapter",
                (BaseSourceAdapter,),
                {
                    "name": "bad-result",
                    "ingest": lambda self, **_kwargs: iter((object(),)),
                    "describe_schema": lambda self: AdapterSchema(version="1.0", fields={}),
                },
            ),
            SourceAdapterProtocolError,
        ),
    ],
)
def test_adapter_protocol_rejects_invalid_metadata_and_unknown_results(
    monkeypatch, adapter_name, adapter_cls, error_type
):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register(adapter_name, adapter_cls)
    with pytest.raises(error_type):
        cli.mine_source_adapter(
            source_name=adapter_name, source_path="/source", palace_path="/fake/palace"
        )


def test_adapter_dry_run_never_initializes_writable_storage_or_kg(monkeypatch):
    class DryRunAdapter(BaseSourceAdapter):
        name = "dry-run"

        def ingest(self, *, source, palace):
            # The supported facade methods remain side-effect-free in dry run.
            palace.upsert_drawer(DrawerRecord(content="helper preview", source_file="fixture://helper"))
            palace.knowledge_graph.add_triple("a", "b", "c")
            yield DrawerRecord(content="preview", source_file="fixture://preview")

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    from mempalace import knowledge_graph, palace

    monkeypatch.setattr(palace, "get_collection", lambda *_args: pytest.fail("opened collection"))
    monkeypatch.setattr(
        knowledge_graph, "KnowledgeGraph", lambda *_args, **_kwargs: pytest.fail("opened KG")
    )
    monkeypatch.setattr(
        palace, "mine_palace_lock", lambda *_args: pytest.fail("acquired mine lock")
    )
    register("dry-run", DryRunAdapter)

    assert (
        cli.mine_source_adapter(
            source_name="dry-run", source_path="/source", palace_path="/fake/palace", dry_run=True
        )
        == 2
    )


def test_adapter_schema_is_validated_before_opening_writable_storage(monkeypatch):
    class InvalidSchemaAdapter(BaseSourceAdapter):
        name = "invalid-schema-before-storage"

        def ingest(self, *, source, palace):
            yield DrawerRecord(content="never reached", source_file="fixture://bad")

        def describe_schema(self):
            return object()

    from mempalace import knowledge_graph, palace

    monkeypatch.setattr(palace, "get_collection", lambda *_args: pytest.fail("opened collection"))
    monkeypatch.setattr(
        knowledge_graph, "KnowledgeGraph", lambda *_args, **_kwargs: pytest.fail("opened KG")
    )
    register("invalid-schema-before-storage", InvalidSchemaAdapter)

    with pytest.raises(SchemaConformanceError):
        cli.mine_source_adapter(
            source_name="invalid-schema-before-storage",
            source_path="/source",
            palace_path="/fake/palace",
        )


def test_adapter_writes_hold_palace_lock_but_dry_run_does_not(monkeypatch):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("fixture", _FixtureAdapter)
    from mempalace import palace

    calls = []

    @contextlib.contextmanager
    def lock(path):
        calls.append(path)
        yield

    monkeypatch.setattr(palace, "mine_palace_lock", lock)
    cli.mine_source_adapter(
        source_name="fixture", source_path="/source", palace_path="/fake/palace"
    )
    cli.mine_source_adapter(
        source_name="fixture", source_path="/source", palace_path="/fake/palace", dry_run=True
    )
    assert calls == ["/fake/palace"]


def test_cli_forwards_uri_and_generic_options_to_adapter(monkeypatch):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("fixture", _FixtureAdapter)
    args = _mine_args(source="fixture")
    args.dir = "slack://workspace/channel"
    args.source_uri = True
    args.source_option = ["limit=3", "label=eng"]

    cli.cmd_mine(args)

    source = _FixtureAdapter.instances[0].source
    assert source.local_path is None
    assert source.uri == "slack://workspace/channel"
    assert source.options == {"limit": 3, "label": "eng"}


def test_cli_rejects_secret_like_source_option_before_adapter_invocation(monkeypatch, capsys):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("fixture", _FixtureAdapter)
    args = _mine_args(source="fixture")
    args.source_option = ["api_token=do-not-put-this-in-argv"]

    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_mine(args)

    assert excinfo.value.code == 2
    assert "looks secret-like" in capsys.readouterr().err
    assert _FixtureAdapter.instances == []


def test_adapter_identity_must_match_registered_namespace_before_ingest(monkeypatch):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    # The same class registered under an alias must not be able to use its
    # declared name to read/delete drawers from the other namespace.
    register("fixture-alias", _FixtureAdapter)

    with pytest.raises(SourceAdapterProtocolError, match="must declare matching name"):
        cli.mine_source_adapter(
            source_name="fixture-alias", source_path="/source", palace_path="/fake/palace"
        )

    assert collection.upserts == []
    assert collection.deletes == []


def test_adapter_privacy_floor_rejects_before_ingest_and_writes(monkeypatch):
    class RestrictedAdapter(_FixtureAdapter):
        name = "restricted"
        default_privacy_class = "pii_potential"
        called = False

        def ingest(self, *, source, palace):
            self.__class__.called = True
            yield from super().ingest(source=source, palace=palace)

    class StrictConfig(_FakeConfig):
        privacy_floor = "internal"
        source_privacy_classes = {}

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    monkeypatch.setattr(cli, "MempalaceConfig", StrictConfig)
    from mempalace import palace

    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda _path: pytest.fail("privacy-rejected source must not initialize storage"),
    )
    register("restricted", RestrictedAdapter)

    with pytest.raises(PrivacyClassRejectedError) as excinfo:
        cli.mine_source_adapter(
            source_name="restricted", source_path="/source", palace_path="/fake/palace"
        )

    assert excinfo.value.privacy_class == "pii_potential"
    assert excinfo.value.privacy_floor == "internal"
    assert RestrictedAdapter.called is False
    assert collection.upserts == []
    assert collection.deletes == []


def test_adapter_persists_universal_metadata_and_route_hints(monkeypatch):
    class RoutedAdapter(BaseSourceAdapter):
        name = "routed"
        adapter_version = "7.2.0"
        default_privacy_class = "internal"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(
                source_file="routed://item",
                version="v1",
                route_hint=RouteHint(wing="item-wing", room="item-room", hall="item-hall"),
            )
            yield DrawerRecord(
                content="routed",
                source_file="routed://item",
                metadata={"privacy_class": "public", "added_by": "forged"},
                route_hint=RouteHint(room="drawer-room"),
            )

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("routed", RoutedAdapter)

    cli.mine_source_adapter(
        source_name="routed",
        source_path="/source",
        palace_path="/fake/palace",
        agent="kit",
    )

    metadata = collection.upserts[0]["metadatas"][0]
    assert metadata["adapter_name"] == "routed"
    assert metadata["adapter_version"] == "7.2.0"
    assert metadata["privacy_class"] == "internal"
    assert metadata["added_by"] == "kit"
    assert metadata["source_file"] == "routed://item"
    assert metadata["chunk_index"] == 0
    assert metadata["wing"] == "item-wing"
    assert metadata["room"] == "drawer-room"
    assert metadata["hall"] == "item-hall"
    assert metadata["filed_at"]


def test_cli_normalizes_local_adapter_paths_but_leaves_uris_verbatim(monkeypatch, tmp_path):
    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("fixture", _FixtureAdapter)
    monkeypatch.chdir(tmp_path)

    args = _mine_args(source="fixture")
    args.dir = "relative-source"
    cli.cmd_mine(args)

    source = _FixtureAdapter.instances[0].source
    assert source.local_path == str((tmp_path / "relative-source").resolve())
    assert source.uri is None


def test_daemon_forwards_wing_as_adapter_option(monkeypatch):
    from mempalace import service

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", os.environ.get("MEMPALACE_PALACE_PATH", ""))
    monkeypatch.setattr(service, "MempalaceConfig", _FakeConfig)
    captured = {}
    monkeypatch.setattr(
        cli,
        "mine_source_adapter",
        lambda **kwargs: captured.update(kwargs) or 1,
    )

    result = service.run_mine(
        {
            "source": "github.com/org/repo",
            "source_adapter": "fixture",
            "source_uri": True,
            "source_options": {"limit": 5},
            "wing": "engineering",
            "palace_path": "/fake",
        }
    )

    assert result["success"] is True
    assert captured["source_is_uri"] is True
    assert captured["source_options"] == {"limit": 5, "wing": "engineering"}


def test_daemon_maps_adapter_errors_like_cli(monkeypatch):
    from mempalace import service

    # ``run_mine`` sets this per-job process environment variable directly;
    # have monkeypatch restore its prior value after this direct unit call.
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", os.environ.get("MEMPALACE_PALACE_PATH", ""))
    monkeypatch.setattr(service, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(
        cli,
        "mine_source_adapter",
        lambda **_kwargs: (_ for _ in ()).throw(UnknownSourceAdapterError("missing")),
    )
    unknown = service.run_mine({"source": "x", "source_adapter": "missing", "palace_path": "/fake"})
    assert unknown["error_class"] == "UnknownSourceAdapterError"
    assert unknown["exit_code"] == 2

    monkeypatch.setattr(
        cli,
        "mine_source_adapter",
        lambda **_kwargs: (_ for _ in ()).throw(SchemaConformanceError("bad metadata")),
    )
    invalid = service.run_mine({"source": "x", "source_adapter": "fixture", "palace_path": "/fake"})
    assert invalid["error_class"] == "SchemaConformanceError"
    assert invalid["exit_code"] == 1


def test_interleaved_source_items_keep_independent_skip_replace_and_route_state(monkeypatch):
    class InterleavedAdapter(BaseSourceAdapter):
        name = "interleaved"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(
                source_file="item://replace", version="new", route_hint=RouteHint(wing="replace")
            )
            yield SourceItemMetadata(
                source_file="item://skip", version="same", route_hint=RouteHint(wing="skip")
            )
            # A's drawer arrives after B's metadata; a single current-item
            # flag used to incorrectly apply B's skip/route to this record.
            yield DrawerRecord(content="replace", source_file="item://replace")
            yield DrawerRecord(content="skip", source_file="item://skip")

        def is_current(self, *, item, existing_metadata):
            return item.version == "same"

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    collection.existing_metadata = {"version": "old"}
    _install_normal_storage(monkeypatch, collection)
    register("interleaved", InterleavedAdapter)

    assert cli.mine_source_adapter(source_name="interleaved", source_path="/source", palace_path="/fake") == 1
    assert collection.upserts[0]["documents"] == ["replace"]
    assert collection.upserts[0]["metadatas"][0]["wing"] == "replace"
    assert collection.deletes == [
        {"where": {"$and": [{"source_file": "item://replace"}, {"adapter_name": "interleaved"}]}}
    ]


@pytest.mark.parametrize("failure", ["malformed", "exception"])
def test_adapter_reconciliation_stages_all_output_before_replacing_existing_drawers(monkeypatch, failure):
    class FailingAdapter(BaseSourceAdapter):
        name = "staged-failure"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(source_file="item://old", version="new")
            yield DrawerRecord(content="good", source_file="item://old")
            if failure == "malformed":
                yield DrawerRecord(content="bad", source_file="item://old", metadata={"undeclared": "x"})
            raise RuntimeError("adapter transport failed")

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    collection.existing_metadata = {"version": "old"}
    _install_normal_storage(monkeypatch, collection)
    register("staged-failure", FailingAdapter)

    expected = SchemaConformanceError if failure == "malformed" else RuntimeError
    with pytest.raises(expected):
        cli.mine_source_adapter(source_name="staged-failure", source_path="/source", palace_path="/fake")
    # The previous source item remains untouched: no early delete, no partial replacement.
    assert collection.deletes == []
    assert collection.upserts == []


def test_adapter_rejects_invalid_default_and_merged_routes_before_storage_mutation(monkeypatch):
    class InvalidRouteAdapter(BaseSourceAdapter):
        name = "invalid-route"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(source_file="item://route", version="new")
            yield DrawerRecord(
                content="bad route", source_file="item://route", route_hint=RouteHint(room="")
            )

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("invalid-route", InvalidRouteAdapter)
    with pytest.raises(SourceAdapterProtocolError, match="route_hint.room"):
        cli.mine_source_adapter(source_name="invalid-route", source_path="/source", palace_path="/fake")
    assert collection.deletes == []
    assert collection.upserts == []

    with pytest.raises(SourceAdapterProtocolError, match="route_hint.wing"):
        cli.mine_source_adapter(
            source_name="invalid-route",
            source_path="/source",
            palace_path="/fake",
            source_options={"wing": 3},
        )


def test_adapter_collection_facade_blocks_raw_write_bypass(monkeypatch):
    class BypassAdapter(BaseSourceAdapter):
        name = "bypass"

        def ingest(self, *, source, palace):
            palace.drawer_collection.delete(where={})
            yield DrawerRecord(content="never", source_file="item://never")

        def describe_schema(self):
            return AdapterSchema(version="1.0", fields={})

    collection = _FakeCollection()
    _install_normal_storage(monkeypatch, collection)
    register("bypass", BypassAdapter)
    with pytest.raises(SourceAdapterProtocolError, match="raw collection writes"):
        cli.mine_source_adapter(source_name="bypass", source_path="/source", palace_path="/fake")
    assert collection.deletes == []
    assert collection.upserts == []


def test_daemon_source_options_and_privacy_rejection_match_source_entrypoints(monkeypatch):
    from mempalace import service

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", os.environ.get("MEMPALACE_PALACE_PATH", ""))
    monkeypatch.setattr(service, "MempalaceConfig", _FakeConfig)
    invalid = service.run_mine(
        {"source": "x", "source_adapter": "fixture", "source_options": [], "palace_path": "/fake"}
    )
    assert invalid == {
        "success": False,
        "error": "source_options must be an object",
        "error_class": "SourceAdapterProtocolError",
        "exit_code": 2,
    }

    monkeypatch.setattr(
        cli,
        "mine_source_adapter",
        lambda **_kwargs: (_ for _ in ()).throw(
            PrivacyClassRejectedError("blocked", privacy_class="sensitive", privacy_floor="internal")
        ),
    )
    rejected = service.run_mine({"source": "x", "source_adapter": "fixture", "palace_path": "/fake"})
    assert rejected["success"] is True
    assert rejected["exit_code"] == 0
    assert rejected["rejected"][0]["privacy_class"] == "sensitive"
