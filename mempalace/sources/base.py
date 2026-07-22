"""Source adapter contract for MemPalace (RFC 002).

Mirrors what ``mempalace/backends/base.py`` does for the write side: it defines
the read-side surface every source adapter must implement. A source adapter
extracts content from a specific origin (filesystem, git, Slack, Cursor …) and
yields typed records (``SourceItemMetadata`` / ``DrawerRecord``) that core
routes into the palace.

This module is spec scaffolding. The first-party miners (``mempalace/miner.py``
and ``mempalace/convo_miner.py``) are migrated onto it in a follow-up PR;
in this PR we publish the contract so third-party adapters can begin building
against a stable surface.

See ``docs/rfcs/002-source-adapter-plugin-spec.md`` for the authoritative
spec text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import re
from typing import TYPE_CHECKING, ClassVar, Iterator, Literal, Optional

if TYPE_CHECKING:
    from .context import PalaceContext  # noqa: F401  (used in string annotation)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceAdapterError(Exception):
    """Base class for every source-adapter error raised by core."""


class UnknownSourceAdapterError(SourceAdapterError):
    """Raised when an explicitly selected adapter is not registered."""


class SourceAdapterProtocolError(SourceAdapterError):
    """Raised when an adapter violates the RFC 002 ingest protocol."""


class SourceNotFoundError(SourceAdapterError):
    """Raised when a ``SourceRef`` does not resolve to a readable source."""


class AuthRequiredError(SourceAdapterError):
    """Raised when an adapter needs credentials that were not provided.

    The message MUST name the env vars (or other supported mechanism) the
    operator needs to set.
    """


class AdapterClosedError(SourceAdapterError):
    """Raised when an adapter method is called after ``close()``."""


class TransformationViolationError(SourceAdapterError):
    """Raised by the conformance suite when round-tripping a drawer requires
    an undeclared transformation (RFC 002 §7.2–7.3)."""


class SchemaConformanceError(SourceAdapterError):
    """Raised when a ``DrawerRecord.metadata`` violates the adapter schema
    returned by :meth:`BaseSourceAdapter.describe_schema`."""


class PrivacyClassRejectedError(SourceAdapterError):
    """Raised before extraction when a source is below the palace privacy floor."""

    def __init__(self, message: str, *, privacy_class: str | None = None, privacy_floor: str | None = None):
        super().__init__(message)
        self.privacy_class = privacy_class
        self.privacy_floor = privacy_floor


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """A handle to the source a user wants to ingest.

    ``local_path`` is for filesystem-rooted sources (project dir, mbox file).
    ``uri`` is for URL-like references (``github.com/org/repo``,
    ``slack://workspace/channel``).
    ``options`` carries adapter-specific non-secret config. Secrets MUST NOT
    be placed here; see §4.2.
    """

    local_path: Optional[str] = None
    uri: Optional[str] = None
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RouteHint:
    """Adapter-supplied routing hint (RFC 002 §2.5)."""

    wing: Optional[str] = None
    room: Optional[str] = None
    hall: Optional[str] = None


@dataclass(frozen=True)
class SourceItemMetadata:
    """Lightweight pointer yielded before drawers for lazy-fetch adapters.

    Core inspects ``version`` via :meth:`BaseSourceAdapter.is_current` to
    decide whether to skip extraction; an adapter that responds positively
    stops yielding drawers for this item and moves to the next.
    """

    source_file: str
    version: str
    size_hint: Optional[int] = None
    route_hint: Optional[RouteHint] = None


@dataclass(frozen=True)
class DrawerRecord:
    """One drawer's worth of extracted content plus flat metadata.

    ``metadata`` values MUST be flat scalars (``str``/``int``/``float``/``bool``)
    per RFC 001 §1.4 — the chroma constraint. Nested data belongs on the
    knowledge graph (§5.5) or in a declared ``json_string`` field (§5.4).
    """

    content: str
    source_file: str
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)
    route_hint: Optional[RouteHint] = None


@dataclass(frozen=True)
class SourceSummary:
    """High-level description of a source returned by :meth:`source_summary`."""

    description: str
    item_count: Optional[int] = None


IngestMode = Literal["chunked_content", "whole_record", "metadata_only"]


@dataclass(frozen=True)
class FieldSpec:
    """Declared shape of a single per-adapter metadata field (§5.2)."""

    type: Literal["string", "int", "float", "bool", "delimiter_joined_string", "json_string"]
    required: bool
    description: str
    indexed: bool = False
    delimiter: str = ";"
    json_schema: Optional[dict] = None


@dataclass(frozen=True)
class AdapterSchema:
    """The per-adapter metadata schema returned by :meth:`describe_schema`."""

    fields: dict[str, FieldSpec]
    version: str


_METADATA_SCALAR_TYPES = (str, int, float, bool)

# Ordered from least to most restricted.  A palace floor admits its own level
# and every level to its left (RFC 002 §6.2).
PRIVACY_CLASSES = (
    "public",
    "internal",
    "pii_potential",
    "sensitive",
    "secrets_possible",
)

# ``SourceRef.options`` is deliberately non-secret.  CLI options are visible
# in shell history and process argv, but rejecting these names at the shared
# boundary also keeps daemon/MCP callers from accidentally normalizing secret
# passing as an adapter API.
_SECRET_OPTION_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|auth(?:orization)?|credential|password|passwd|secret|token|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)


def validate_source_options(options: dict) -> None:
    """Reject malformed or secret-like adapter option keys before ingestion."""
    if not isinstance(options, dict):
        raise SourceAdapterProtocolError("source_options must be a dict")
    for key in options:
        if not isinstance(key, str) or not key:
            raise SourceAdapterProtocolError("source option keys must be non-empty strings")
        # Also catch compact spellings such as ``apiKey`` and ``accessToken``.
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if _SECRET_OPTION_KEY_RE.search(key) or any(
            marker in normalized
            for marker in ("apikey", "authtoken", "accesstoken", "credential", "password", "passwd", "secret", "token", "privatekey")
        ):
            raise SourceAdapterProtocolError(
                f"source option {key!r} looks secret-like; provide credentials through environment variables"
            )


def validate_privacy_class(value: str, *, field_name: str = "privacy_class") -> str:
    if not isinstance(value, str) or value not in PRIVACY_CLASSES:
        allowed = ", ".join(PRIVACY_CLASSES)
        raise SourceAdapterProtocolError(f"{field_name} must be one of: {allowed}")
    return value


def privacy_class_is_admitted(privacy_class: str, privacy_floor: str | None) -> bool:
    """Return whether ``privacy_class`` is admitted by an optional floor."""
    validate_privacy_class(privacy_class)
    if privacy_floor is None:
        return True
    validate_privacy_class(privacy_floor, field_name="privacy_floor")
    return PRIVACY_CLASSES.index(privacy_class) <= PRIVACY_CLASSES.index(privacy_floor)


def validate_route_hint(route_hint: RouteHint | None) -> None:
    """Validate the flat routing surface adapters are allowed to provide."""
    if route_hint is None:
        return
    if not isinstance(route_hint, RouteHint):
        raise SourceAdapterProtocolError("route_hint must be a RouteHint or None")
    for field_name in ("wing", "room", "hall"):
        value = getattr(route_hint, field_name)
        if value is not None and (not isinstance(value, str) or not value):
            raise SourceAdapterProtocolError(
                f"route_hint.{field_name} must be a non-empty string or None"
            )


def validate_adapter_schema(schema: AdapterSchema) -> None:
    """Validate the portion of an RFC 002 schema core relies on at runtime.

    Adapters are third-party code, so ``describe_schema`` cannot be trusted to
    have constructed the dataclasses exactly as documented.  Fail before
    extraction rather than allowing malformed metadata into a backend.
    """
    if not isinstance(schema, AdapterSchema) or not isinstance(schema.version, str):
        raise SchemaConformanceError(
            "describe_schema() must return an AdapterSchema with a string version"
        )
    if not isinstance(schema.fields, dict):
        raise SchemaConformanceError("AdapterSchema.fields must be a dict")
    for name, field_spec in schema.fields.items():
        if not isinstance(name, str) or not name:
            raise SchemaConformanceError("AdapterSchema field names must be non-empty strings")
        if not isinstance(field_spec, FieldSpec):
            raise SchemaConformanceError(f"schema field {name!r} must be a FieldSpec")
        if field_spec.type not in {
            "string",
            "int",
            "float",
            "bool",
            "delimiter_joined_string",
            "json_string",
        }:
            raise SchemaConformanceError(
                f"schema field {name!r} has unsupported type {field_spec.type!r}"
            )


def validate_drawer_metadata(metadata: dict, schema: AdapterSchema) -> None:
    """Reject non-flat or schema-incompatible metadata before it is persisted."""
    validate_adapter_schema(schema)
    if not isinstance(metadata, dict):
        raise SchemaConformanceError("DrawerRecord.metadata must be a dict")

    for name, value in metadata.items():
        if not isinstance(name, str) or not name:
            raise SchemaConformanceError("DrawerRecord.metadata keys must be non-empty strings")
        # ``bool`` is deliberately accepted as a scalar, even though it is an
        # ``int`` subclass. Field-specific checks below use exact types.
        if type(value) not in _METADATA_SCALAR_TYPES:
            raise SchemaConformanceError(
                f"metadata field {name!r} must be a flat str/int/float/bool value"
            )

    for name, field_spec in schema.fields.items():
        if name not in metadata:
            if field_spec.required:
                raise SchemaConformanceError(f"metadata is missing required field {name!r}")
            continue
        value = metadata[name]
        expected = field_spec.type
        valid = (
            (
                expected in {"string", "delimiter_joined_string", "json_string"}
                and type(value) is str
            )
            or (expected == "int" and type(value) is int)
            or (expected == "float" and type(value) is float)
            or (expected == "bool" and type(value) is bool)
        )
        if not valid:
            raise SchemaConformanceError(
                f"metadata field {name!r} must be {expected}, got {type(value).__name__}"
            )
        if expected == "json_string":
            try:
                json.loads(value)
            except (TypeError, ValueError) as exc:
                raise SchemaConformanceError(
                    f"metadata field {name!r} must contain valid JSON"
                ) from exc


# The union type adapters yield from ``ingest``.
IngestResult = object  # intentionally broad; runtime checks in core


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


class BaseSourceAdapter(ABC):
    """Long-lived adapter serving many ``SourceRef`` invocations (RFC 002 §2).

    Instances are lightweight on construction — no I/O, no network, no
    credential fetch. All work is deferred to :meth:`ingest`. Instances are
    thread-safe for concurrent ``ingest`` calls across different ``SourceRef``
    values (v1 serializes within a single ``SourceRef``).

    Class attributes form the adapter's identity contract:

    * ``name`` — stable adapter name used for registration and drawer metadata.
    * ``adapter_version`` — adapter's own version, independent of
      ``spec_version``. Recorded on every drawer so re-extract workflows can
      target drawers from a known-buggy adapter version.
    * ``capabilities`` — free-form tokens; core inspects a documented subset.
    * ``supported_modes`` — subset of ``chunked_content``, ``whole_record``,
      ``metadata_only``.
    * ``declared_transformations`` — set of transformation names the adapter
      applies to source bytes. The empty set marks a byte-preserving adapter.
    * ``default_privacy_class`` — privacy class level (§6) applied unless the
      palace config overrides it.
    """

    name: ClassVar[str]
    spec_version: ClassVar[str] = "1.0"
    adapter_version: ClassVar[str] = "0.0.0"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    supported_modes: ClassVar[frozenset[str]] = frozenset({"chunked_content"})
    declared_transformations: ClassVar[frozenset[str]] = frozenset()
    default_privacy_class: ClassVar[str] = "pii_potential"

    # ------------------------------------------------------------------
    # Required methods
    # ------------------------------------------------------------------

    @abstractmethod
    def ingest(
        self,
        *,
        source: SourceRef,
        palace: "PalaceContext",
    ) -> Iterator[IngestResult]:
        """Enumerate and extract content from a source.

        Yields a stream of ``SourceItemMetadata`` and ``DrawerRecord`` values.
        Lazy adapters yield ``SourceItemMetadata`` ahead of the drawers for
        that item so core can check :meth:`is_current` before committing to
        the fetch. Eager adapters MAY interleave freely.
        """

    @abstractmethod
    def describe_schema(self) -> AdapterSchema:
        """Declare the structured metadata this adapter attaches.

        The returned schema MUST be stable for a given ``adapter_version``.
        Enterprises index on it; core uses it to validate adapter output.
        """

    # ------------------------------------------------------------------
    # Optional methods with default implementations
    # ------------------------------------------------------------------

    def is_current(
        self,
        *,
        item: SourceItemMetadata,
        existing_metadata: Optional[dict],
    ) -> bool:
        """Return True if the palace already has an up-to-date copy of ``item``.

        Default: always returns False (re-extract every time). Adapters
        advertising ``supports_incremental`` MUST override.
        """
        return False

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        """Describe a source without extracting."""
        return SourceSummary(description=self.name)

    def close(self) -> None:
        """Release any resources the adapter holds. Default: no-op."""
        return None
