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
from itertools import count
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol

from .base import (
    DrawerRecord,
    RouteHint,
    SourceAdapterProtocolError,
    validate_privacy_class,
    validate_route_hint,
)


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


def _freeze_config_value(value: Any) -> Any:
    """Make the small, adapter-safe config projection immutable."""
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config_value(item) for item in value)
    return value


@dataclass(frozen=True)
class AdapterConfig:
    """Explicit non-secret config surface available to source adapters.

    Passing ``MempalaceConfig`` through the plugin boundary exposes private
    file contents and backend credentials. Adapters only receive routing data
    and the already-enforced privacy policy as immutable snapshots.
    """

    hall_keywords: Mapping[str, Any]
    topic_wings: tuple[Any, ...]
    privacy_floor: str | None

    @classmethod
    def from_config(cls, config: Any) -> "AdapterConfig":
        return cls(
            hall_keywords=_freeze_config_value(getattr(config, "hall_keywords", {})),
            topic_wings=_freeze_config_value(getattr(config, "topic_wings", ())),
            privacy_floor=getattr(config, "privacy_floor", None),
        )


class ReadOnlyKnowledgeGraph:
    """Deliberately capability-poor KG surface for third-party adapters.

    RFC 002 dispatch does not give plugins a live KG/backend handle. The
    mutating KG API is intentionally absent; callers can use the stable read
    summary only. This prevents a plugin from committing a graph transaction
    while its drawer output is still staged.
    """

    __slots__ = ("_stats",)

    def __init__(self, knowledge_graph: Any):
        stats = getattr(knowledge_graph, "stats", None)
        value = stats() if callable(stats) else {}
        self._stats = MappingProxyType(dict(value)) if isinstance(value, dict) else MappingProxyType({})

    def stats(self) -> dict:
        """Return a detached graph summary when the backend supports it."""
        return dict(self._stats)


# Progress hook signature: ``fn(event_name, **details) -> None``.
ProgressHook = Callable[..., None]


class _AdapterCollectionFacade:
    """Read-only collection view exposed to adapters.

    Source adapters must use :meth:`PalaceContext.upsert_drawer` and the core
    reconciliation loop for writes.  Returning the backend collection directly
    let an adapter bypass metadata stamps, schema validation, adapter scoping,
    and staged replacement entirely.
    """

    __slots__ = ("_context_id",)

    def __init__(self, context_id: int):
        self._context_id = context_id

    def get(self, **kwargs: Any) -> Any:
        return _storage_for(self._context_id).get(**kwargs)

    def query(self, **kwargs: Any) -> Any:
        return _storage_for(self._context_id).query(**kwargs)

    def count(self) -> int:
        return _storage_for(self._context_id).count()

    def _reject_write(self, *_args: Any, **_kwargs: Any) -> None:
        raise SourceAdapterProtocolError(
            "adapters must use PalaceContext.upsert_drawer; raw collection writes are not allowed"
        )

    add = _reject_write
    upsert = _reject_write
    delete = _reject_write


# Keep the raw backend outside the adapter-visible context object.  Python
# plugins run in-process and are not a security sandbox, but this capability
# boundary prevents accidental or obvious-private-attribute bypasses of the
# source reconciliation protocol. Entries are released at the end of mine.
_CONTEXT_STORAGE: dict[int, _CollectionLike] = {}
_CONTEXT_IDS = count(1)


def _storage_for(context_id: int) -> _CollectionLike:
    try:
        return _CONTEXT_STORAGE[context_id]
    except KeyError as exc:  # pragma: no cover - defensive lifecycle guard
        raise SourceAdapterProtocolError("PalaceContext storage is no longer available") from exc


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
    staging: bool = False

    # Internal: flag set by :meth:`skip_current_item` and checked by the core
    # mine loop between yields. Not part of the adapter-facing contract; the
    # adapter only needs to know that calling :meth:`skip_current_item` stops
    # drawer emission for the current ``SourceItemMetadata``.
    _skip_requested: bool = False
    _staged_drawers: list[DrawerRecord] = field(default_factory=list, init=False, repr=False)
    _staged_deletes: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        # Keep the construction signature stable while replacing the public
        # backend handle with a constrained view before any adapter sees it.
        # ``id(self)`` is reusable after GC. A monotonic opaque lease means a
        # retained facade cannot resolve to a later invocation's backend.
        self._context_storage_id = next(_CONTEXT_IDS)
        _CONTEXT_STORAGE[self._context_storage_id] = self.drawer_collection
        self.drawer_collection = _AdapterCollectionFacade(self._context_storage_id)

    def _release_core_storage(self) -> None:
        """Core lifecycle hook; adapters never receive the backend handle."""
        context_id = getattr(self, "_context_storage_id", None)
        if context_id is not None:
            _CONTEXT_STORAGE.pop(context_id, None)
            self._context_storage_id = None

    # ------------------------------------------------------------------
    # Adapter-facing surface
    # ------------------------------------------------------------------

    def upsert_drawer(self, record: DrawerRecord) -> None:
        """Persist an adapter drawer using its stable, public ID shape."""
        self._upsert_drawer(record)

    def _upsert_drawer(
        self, record: DrawerRecord, *, id_suffix: str = "", generation: str | None = None
    ) -> None:
        """Core-only drawer persistence with an optional candidate ID suffix."""
        """Persist a ``DrawerRecord`` to the drawer collection.

        Applies the spec-mandated ``adapter_name`` and ``adapter_version``
        metadata stamps (§5.1) so adapters never need to populate them.
        """
        if self.staging:
            # Preserve adapter provenance. Core validates helper writes just
            # like yielded records after extraction; pre-merging this hint
            # used to make helper records look core-routed.
            self._staged_drawers.append(record)
            return
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
        if generation is not None:
            meta["source_generation"] = generation
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
        drawer_id = _build_drawer_id(
            record, adapter_name=self.adapter_name, id_suffix=id_suffix
        )
        validate_route_hint(self.default_route_hint)
        validate_route_hint(record.route_hint)
        validate_route_hint(route)
        _storage_for(self._context_storage_id).upsert(
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
        result = _storage_for(self._context_storage_id).get(where=self._source_item_where(source_file), limit=1)
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
        if self.staging:
            self._staged_deletes.add(source_file)
        elif not self.dry_run:
            _storage_for(self._context_storage_id).delete(where=self._source_item_where(source_file))

    def source_item_ids(self, source_file: str) -> list[str]:
        """Return the exact durable IDs for this adapter-scoped source item.

        Replacement uses these IDs only after all candidate drawers have been
        durably written. Never delete by a broad source filter after candidate
        writes: that could erase the just-written generation too.
        """
        result = _storage_for(self._context_storage_id).get(
            where=self._source_item_where(source_file), include=[]
        )
        if not isinstance(result, dict):
            return []
        return [value for value in (result.get("ids") or []) if isinstance(value, str)]

    def _drawer_id_for(self, record: DrawerRecord, *, id_suffix: str = "") -> str:
        """Core-only deterministic ID calculation for rollback bookkeeping."""
        return _build_drawer_id(
            record, adapter_name=self.adapter_name, id_suffix=id_suffix
        )

    def _delete_drawer_ids(self, ids: list[str]) -> None:
        """Core-only exact-ID deletion used by durable reconciliation."""
        if ids and not self.dry_run:
            _storage_for(self._context_storage_id).delete(ids=ids)

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

    def drain_staged_mutations(self) -> tuple[list[DrawerRecord], set[str]]:
        """Return staged adapter helper mutations after a successful ingest.

        This is intentionally a core-only escape hatch despite being public
        Python for testability.  It never exposes the raw backend collection.
        """
        drawers, deletes = self._staged_drawers, self._staged_deletes
        self._staged_drawers, self._staged_deletes = [], set()
        return drawers, deletes

    def emit(self, event: str, **details: Any) -> None:
        """Invoke each registered progress hook with ``(event, **details)``."""
        for hook in self.progress_hooks:
            try:
                hook(event, **details)
            except Exception:  # pragma: no cover - hook errors never fail mine
                import logging

                logging.getLogger(__name__).exception("progress hook failed on %r", event)


def _build_drawer_id(
    record: DrawerRecord, *, adapter_name: str = "", id_suffix: str = ""
) -> str:
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
    return f"{digest}_{record.chunk_index}{id_suffix}"


def _merge_route_hints(default: Optional[RouteHint], record: Optional[RouteHint]) -> RouteHint:
    """Overlay a per-drawer hint over the current item/default route."""
    default = default or RouteHint(wing="default", room="general", hall="general")
    record = record or RouteHint()
    return RouteHint(
        wing=record.wing if record.wing is not None else default.wing,
        room=record.room if record.room is not None else default.room,
        hall=record.hall if record.hall is not None else default.hall,
    )
