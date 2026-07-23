"""RFC 002 §7 content conformance helpers for adapter fixture suites.

Core dispatch cannot run these checks because canonical source bytes are
adapter- and fixture-specific.  Adapter packages call this helper from their
own conformance tests with captured canonical bytes and the records they
produced.  A transformation order is explicit because RFC 002's public
``frozenset`` declaration intentionally has no iteration order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .base import DrawerRecord, TransformationViolationError
from .transforms import RESERVED_TRANSFORMATIONS


def _ordered_records(records: Iterable[DrawerRecord]) -> dict[str, list[DrawerRecord]]:
    grouped: dict[str, list[DrawerRecord]] = defaultdict(list)
    for record in records:
        if not isinstance(record, DrawerRecord):
            raise TransformationViolationError("conformance records must be DrawerRecord instances")
        # RFC 002 §1.5 / §7: metadata-only records have no source-content
        # round trip to verify.
        if record.metadata.get("ingest_mode") == "metadata_only":
            continue
        grouped[record.source_file].append(record)
    return {source_file: sorted(items, key=lambda record: record.chunk_index)
            for source_file, items in grouped.items()}


def _apply_declared_transformations(
    adapter: Any, raw: bytes, transformation_order: tuple[str, ...]
) -> str:
    declared = getattr(adapter, "declared_transformations", frozenset())
    if frozenset(transformation_order) != declared or len(transformation_order) != len(declared):
        raise TransformationViolationError(
            "transformation_order must list each declared transformation exactly once"
        )
    value: bytes | str = raw
    for name in transformation_order:
        reference_name = f"{adapter.name.replace('-', '_')}_{name.replace('-', '_')}"
        transform = getattr(__import__("mempalace.sources.transforms", fromlist=[reference_name]), reference_name, None)
        if not callable(transform):
            transform = RESERVED_TRANSFORMATIONS.get(name)
        if not callable(transform):
            raise TransformationViolationError(f"no reference implementation for declared transformation {name!r}")
        if isinstance(value, bytes) and name != "utf8_replace_invalid":
            value = value.decode("utf-8")
        value = transform(value)
    return value if isinstance(value, str) else value.decode("utf-8")


def assert_content_round_trip(
    adapter: Any,
    canonical_source_bytes: Mapping[str, bytes],
    records: Iterable[DrawerRecord],
    *,
    transformation_order: tuple[str, ...] = (),
) -> None:
    """Raise when an adapter's emitted content exceeds its declared contract.

    ``byte_preserving`` adapters must match the canonical UTF-8 text exactly.
    Lossy adapters must match only the supplied ordered reference pipeline;
    callers therefore cannot claim a lossy transform without a reproducible
    implementation.  The helper is designed for adapter package fixture tests,
    where canonical API/file bytes are available.
    """
    grouped = _ordered_records(records)
    byte_preserving = "byte_preserving" in getattr(adapter, "capabilities", frozenset())
    declared = getattr(adapter, "declared_transformations", frozenset())
    for source_file, source_records in grouped.items():
        try:
            raw = canonical_source_bytes[source_file]
        except KeyError as exc:
            raise TransformationViolationError(f"missing canonical bytes for {source_file!r}") from exc
        if not isinstance(raw, bytes):
            raise TransformationViolationError(f"canonical bytes for {source_file!r} must be bytes")
        actual = "".join(record.content for record in source_records)
        if byte_preserving:
            if declared:
                raise TransformationViolationError("byte_preserving adapters must not declare transformations")
            expected = raw.decode("utf-8")
            label = "byte_preserving"
        else:
            expected = _apply_declared_transformations(adapter, raw, transformation_order)
            label = "undeclared transformation"
        if actual != expected:
            raise TransformationViolationError(
                f"{label} changed content for {source_file!r}: adapter output does not match canonical pipeline"
            )
