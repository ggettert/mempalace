"""Source adapter subsystem (RFC 002).

Public surface:

* :class:`BaseSourceAdapter` — per-source read-side contract.
* Typed records: :class:`SourceRef`, :class:`SourceItemMetadata`,
  :class:`DrawerRecord`, :class:`RouteHint`, :class:`SourceSummary`,
  :class:`AdapterSchema`, :class:`FieldSpec`.
* Error classes: :class:`SourceNotFoundError`, :class:`AuthRequiredError`,
  :class:`AdapterClosedError`, :class:`TransformationViolationError`,
  :class:`SchemaConformanceError`.
* Registry: :func:`register`, :func:`get_adapter`, :func:`available_adapters`,
  :func:`resolve_adapter_for_source`.
* :class:`PalaceContext` — facade core passes to adapters during ``ingest``.
* :mod:`transforms` — reference implementations of the reserved §1.4
  transformations + :func:`get_transformation` resolver.
"""

from .base import (
    AdapterClosedError,
    AdapterSchema,
    AuthRequiredError,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    IngestMode,
    IngestResult,
    RouteHint,
    PRIVACY_CLASSES,
    PrivacyClassRejectedError,
    SchemaConformanceError,
    SourceAdapterError,
    SourceAdapterProtocolError,
    SourceItemMetadata,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
    TransformationViolationError,
    UnknownSourceAdapterError,
    validate_adapter_schema,
    validate_adapter_contract,
    validate_drawer_metadata,
    validate_drawer_ingest_mode,
    validate_privacy_class,
    validate_route_hint,
    validate_source_options,
    privacy_class_is_admitted,
    UNIVERSAL_METADATA_FIELDS,
)
from .context import AdapterConfig, PalaceContext, ProgressHook, ReadOnlyKnowledgeGraph
from .conformance import assert_content_round_trip
from .registry import (
    available_adapters,
    adapter_session,
    get_adapter,
    get_adapter_class,
    register,
    reset_adapters,
    resolve_adapter_for_source,
    unregister,
)

__all__ = [
    "AdapterClosedError",
    "AdapterConfig",
    "AdapterSchema",
    "AuthRequiredError",
    "BaseSourceAdapter",
    "DrawerRecord",
    "FieldSpec",
    "IngestMode",
    "IngestResult",
    "PalaceContext",
    "ProgressHook",
    "ReadOnlyKnowledgeGraph",
    "RouteHint",
    "PRIVACY_CLASSES",
    "PrivacyClassRejectedError",
    "SchemaConformanceError",
    "SourceAdapterError",
    "SourceAdapterProtocolError",
    "SourceItemMetadata",
    "SourceNotFoundError",
    "SourceRef",
    "SourceSummary",
    "TransformationViolationError",
    "UnknownSourceAdapterError",
    "available_adapters",
    "assert_content_round_trip",
    "adapter_session",
    "get_adapter",
    "get_adapter_class",
    "register",
    "reset_adapters",
    "resolve_adapter_for_source",
    "unregister",
    "validate_adapter_schema",
    "validate_adapter_contract",
    "validate_drawer_metadata",
    "validate_drawer_ingest_mode",
    "validate_privacy_class",
    "validate_route_hint",
    "validate_source_options",
    "privacy_class_is_admitted",
    "UNIVERSAL_METADATA_FIELDS",
]
