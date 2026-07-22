"""CLI coverage for explicit RFC 002 source-adapter dispatch (#2062)."""

import argparse
import sys

import pytest

from mempalace import cli
from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
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

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)


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
    )


def test_cmd_mine_source_dispatches_registered_adapter_through_palace_context(monkeypatch):
    from mempalace import knowledge_graph, palace

    collection = _FakeCollection()
    register("fixture", _FixtureAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "get_collection", lambda palace_path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    cli.cmd_mine(_mine_args(source="fixture"))

    adapter = _FixtureAdapter.instances[0]
    assert adapter.source.local_path == "/source"
    assert adapter.palace.palace_path == "/fake/palace"
    assert adapter.palace.adapter_name == "fixture"
    assert adapter.palace.adapter_version == "0.1.0"
    assert adapter.palace.drawer_collection is collection
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
