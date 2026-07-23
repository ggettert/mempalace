"""Executable RFC 002 §7.2–§7.3 content conformance checks."""

import pytest

from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    TransformationViolationError,
    assert_content_round_trip,
)


class _BytePreserving(BaseSourceAdapter):
    name = "bytes"
    capabilities = frozenset({"byte_preserving"})

    def ingest(self, **_kwargs):
        return iter(())

    def describe_schema(self):
        return AdapterSchema(version="1", fields={})


class _Lossy(BaseSourceAdapter):
    name = "lossy"
    declared_transformations = frozenset({"newline_normalize"})

    def ingest(self, **_kwargs):
        return iter(())

    def describe_schema(self):
        return AdapterSchema(version="1", fields={})


def test_byte_preserving_round_trip_rejects_undeclared_content_change():
    with pytest.raises(TransformationViolationError, match="byte_preserving"):
        assert_content_round_trip(
            _BytePreserving(),
            {"item://one": b"original"},
            [DrawerRecord("changed", "item://one")],
        )


def test_declared_transform_round_trip_accepts_only_the_reference_pipeline():
    adapter = _Lossy()
    assert_content_round_trip(
        adapter,
        {"item://one": b"one\r\ntwo\rthree"},
        [DrawerRecord("one\ntwo\nthree", "item://one")],
        transformation_order=("newline_normalize",),
    )
    with pytest.raises(TransformationViolationError, match="undeclared"):
        assert_content_round_trip(
            adapter,
            {"item://one": b"one\r\ntwo"},
            [DrawerRecord("one two", "item://one")],
            transformation_order=("newline_normalize",),
        )
