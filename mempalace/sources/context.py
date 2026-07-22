"""``PalaceContext`` facade passed to source adapters (RFC 002 §9).

Bundles the palace-side surface an adapter needs during :meth:`ingest`:
drawer collection, closet collection, knowledge graph, palace config, and
progress hooks. Adapters receive a ``PalaceContext`` instance and MUST NOT
import ``mempalace.palace`` directly — that coupling is what the facade
exists to prevent.

This module publishes the shape third-party adapters target. Core's mine
loop will construct a concrete ``PalaceContext`` and pass it to adapters
when the filesystem/conversations miners are migrated onto ``BaseSourceAdapter``
in a follow-up PR; until then, no in-tree code constructs one, but the
contract is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from .base import DrawerRecord, RouteHint, validate_privacy_class


class _CollectionLike(Protocol):
    """Minimum of :class:`mempalace.backends.BaseCollection` adapters rely on.

    Declared as a Protocol so tests and third-party adapters can substitute
    any object with compatible method signatures without importing the
    concrete backend. See ``mempalace/backends/base.py`` for the full surface.
    """

    def add(self, **kwargs: Any) -> None: ...
    def upsert(self, **kwargs: Any) -> None: ...
    def query(self, **kwargs: Any) -> Any: ...
    def get(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> None: ...
    def count(self) -> int: ...


class _KnowledgeGraphLike(Protocol):
    def add_triple(self, subject: str, predicate: str, obj: str, **kwargs: Any) -> Any: ...


# Progress hook signature: ``fn(event_name, **details) -> None``.
ProgressHook = Callable[..., None]


@dataclass
class PalaceContext:
    """Per-mine-invocation facade passed to :meth:`BaseSourceAdapter.ingest`.

    Fields:
        drawer_collection: The palace's drawer collection (via RFC 001 backend).
        closet_collection: The palace's closet collection, or ``None`` if the
            palace has no closets yet. Adapters should not write to this
            directly; core builds closets post-step (RFC 002 §1.7).
        knowledge_graph: The palace's SQLite knowledge graph. Adapters
            advertising ``supports_kg_triples`` call ``add_triple`` on it.
        palace_path: Filesystem root of the palace (convenience; same as
            ``backend.PalaceRef.local_path``).
        config: Palace config object (hall keywords, rooms list, privacy
            floor, etc.). Shape is the existing :class:`MempalaceConfig`.
        adapter_name: Name of the adapter currently ingesting; populated by
            core so drawers can carry ``metadata["adapter_name"]``.
        adapter_version: Version of the adapter currently ingesting.
        progress_hooks: Optional callables core invokes on progress events.

    Methods are intentionally thin wrappers so the concrete mine loop in
    core can swap implementations without changing adapter code.
    """

    drawer_collection: _CollectionLike
    knowledge_graph: _KnowledgeGraphLike
    palace_path: str
    closet_collection: Optional[_CollectionLike] = None
    config: Optional[Any] = None
    adapter_name: str = ""
    adapter_version: str = ""
    added_by: str = "mempalace"
    privacy_class: str = "pii_potential"
    default_route_hint: Optional[RouteHint] = None
    progress_hooks: list[ProgressHook] = field(default_factory=list)
    dry_run: bool = False

    # Internal: flag set by :meth:`skip_current_item` and checked by the core
    # mine loop between yields. Not part of the adapter-facing contract; the
    # adapter only needs to know that calling :meth:`skip_current_item` stops
    # drawer emission for the current ``SourceItemMetadata``.
    _skip_requested: bool = False

    # ------------------------------------------------------------------
    # Adapter-facing surface
    # ------------------------------------------------------------------

    def upsert_drawer(self, record: DrawerRecord) -> None:
        """Persist a ``DrawerRecord`` to the drawer collection.

        Applies the spec-mandated ``adapter_name`` and ``adapter_version``
        metadata stamps (§5.1) so adapters never need to populate them.
        """
        if self.dry_run:
            return
        meta = dict(record.metadata)
        # These are core-owned identity fields.  Do not allow an adapter's
        # arbitrary metadata to make a drawer appear to belong to another
        # source item or chunk.
        meta["source_file"] = record.source_file
        meta["chunk_index"] = record.chunk_index
        if self.adapter_name:
            meta["adapter_name"] = self.adapter_name
        if self.adapter_version:
            meta["adapter_version"] = self.adapter_version
        # Universal fields are core-owned. This keeps a third-party adapter
        # from downgrading its privacy class or forging who/routing metadata.
        meta["privacy_class"] = validate_privacy_class(self.privacy_class)
        meta["added_by"] = self.added_by
        meta["filed_at"] = datetime.now(timezone.utc).isoformat()
        route = _merge_route_hints(self.default_route_hint, record.route_hint)
        # Older adapters may still carry routing in metadata. Preserve that
        # established shape when no RFC RouteHint/default was supplied; new
        # explicit route hints always take precedence.
        if self.default_route_hint is None and record.route_hint is None:
            route = RouteHint(
                wing=meta.get("wing") or route.wing,
                room=meta.get("room") or route.room,
                hall=meta.get("hall") or route.hall,
            )
        meta["wing"] = route.wing or "default"
        meta["room"] = route.room or "general"
        meta["hall"] = route.hall or "general"
        drawer_id = _build_drawer_id(record, adapter_name=self.adapter_name)
        self.drawer_collection.upsert(
            documents=[record.content],
            ids=[drawer_id],
            metadatas=[meta],
        )

    def existing_metadata(self, source_file: str) -> Optional[dict]:
        """Return one stored metadata record for an RFC 002 source item.

        The source item, rather than a separate cursor table, is the
        incremental-ingest cursor.  A dry-run context deliberately has a
        no-op collection and therefore reports no prior state.
        """
        result = self.drawer_collection.get(where=self._source_item_where(source_file), limit=1)
        if not isinstance(result, dict):
            return None
        metadata = result.get("metadatas") or []
        return metadata[0] if metadata and isinstance(metadata[0], dict) else None

    def delete_source_item(self, source_file: str) -> None:
        """Purge all drawers for a tombstoned source item.

        ``source_file`` is adapter-defined, not a globally namespaced ID.
        Scope the mutation to this adapter so two adapters that intentionally
        ingest the same logical source do not delete each other's drawers.
        """
        if not self.dry_run:
            self.drawer_collection.delete(where=self._source_item_where(source_file))

    def replace_source_item(self, source_file: str) -> None:
        """Clear a stale source item before writing its replacement drawers."""
        self.delete_source_item(source_file)

    def _source_item_where(self, source_file: str) -> dict:
        """Return the backend filter identifying this adapter's source item."""
        if not self.adapter_name:
            return {"source_file": source_file}
        return {
            "$and": [
                {"source_file": source_file},
                {"adapter_name": self.adapter_name},
            ]
        }

    def skip_current_item(self) -> None:
        """Signal to core that the current ``SourceItemMetadata`` is up-to-date
        and no drawers should be emitted for it. Core resets the flag after
        advancing past the item."""
        self._skip_requested = True

    def emit(self, event: str, **details: Any) -> None:
        """Invoke each registered progress hook with ``(event, **details)``."""
        for hook in self.progress_hooks:
            try:
                hook(event, **details)
            except Exception:  # pragma: no cover - hook errors never fail mine
                import logging

                logging.getLogger(__name__).exception("progress hook failed on %r", event)


def _build_drawer_id(record: DrawerRecord, *, adapter_name: str = "") -> str:
    """Deterministic drawer id: ``<sha256(adapter + source_file)[:24]>_<chunk_index>``.

    Matches the shape existing miners rely on (``source_file`` + chunk index
    pair) while keeping the id chroma-safe (no separators that collide with
    existing metadata values). 96-bit SHA-256 prefix keeps collision risk
    negligible across corpora the size of a palace (sha1@64 bits was too
    close to the birthday bound for large ingests). Adapters that need a
    different id scheme can bypass :meth:`PalaceContext.upsert_drawer` and
    write through ``drawer_collection.upsert`` directly.
    """
    import hashlib

    identity = record.source_file
    if adapter_name:
        # Source identities are scoped to their adapter under RFC 002.  The
        # separator makes ("ab", "c") distinct from ("a", "bc").
        identity = f"{adapter_name}\0{record.source_file}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{digest}_{record.chunk_index}"


def _merge_route_hints(default: Optional[RouteHint], record: Optional[RouteHint]) -> RouteHint:
    """Overlay a per-drawer hint over the current item/default route."""
    default = default or RouteHint(wing="default", room="general", hall="general")
    record = record or RouteHint()
    return RouteHint(
        wing=record.wing if record.wing is not None else default.wing,
        room=record.room if record.room is not None else default.room,
        hall=record.hall if record.hall is not None else default.hall,
    )
