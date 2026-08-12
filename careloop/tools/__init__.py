"""Tools: the registry that owns argument classes, the tool bodies, and the executor."""

from careloop.tools.callables import (
    ToolBindingError,
    ToolSet,
    UnknownTool,
    bound_method_name,
    bound_tools,
    implements,
)
from careloop.tools.document_source import (
    DOCUMENT_SOURCES,
    DocumentKind,
    DocumentSource,
)
from careloop.tools.documents import (
    POISONED_DOCUMENT_ID,
    RECEIPT_DOCUMENT_ID,
    DocumentNotFound,
    DocumentStore,
    default_document_store,
    fixture_documents,
)
from careloop.tools.executor import (
    ExecutionOutcome,
    ExecutionResult,
    IdempotencyKeyCollision,
    LedgerEntry,
    MockExternalExecutor,
    UnknownActionType,
)
from careloop.tools.idempotency import (
    UncanonicalizableArgument,
    args_digest,
    canonical_args,
    idempotency_key,
)
from careloop.tools.pdf import (
    DocumentUnreadable,
    extract_pages,
    read_document,
)
from careloop.tools.registry import (
    REGISTRY,
    ArgSpec,
    DuplicateToolName,
    ToolRegistry,
    ToolSpec,
)
from careloop.tools.results import (
    AccessGranted,
    ActionResult,
    AppointmentBooked,
    Document,
    DocumentUploaded,
    EmailSent,
    ToolResult,
)

__all__ = [
    "DOCUMENT_SOURCES",
    "POISONED_DOCUMENT_ID",
    "RECEIPT_DOCUMENT_ID",
    "REGISTRY",
    "AccessGranted",
    "ActionResult",
    "AppointmentBooked",
    "ArgSpec",
    "Document",
    "DocumentKind",
    "DocumentNotFound",
    "DocumentSource",
    "DocumentStore",
    "DocumentUnreadable",
    "DocumentUploaded",
    "DuplicateToolName",
    "EmailSent",
    "ExecutionOutcome",
    "ExecutionResult",
    "IdempotencyKeyCollision",
    "LedgerEntry",
    "MockExternalExecutor",
    "ToolBindingError",
    "ToolRegistry",
    "ToolResult",
    "ToolSet",
    "ToolSpec",
    "UncanonicalizableArgument",
    "UnknownActionType",
    "UnknownTool",
    "args_digest",
    "bound_method_name",
    "bound_tools",
    "canonical_args",
    "default_document_store",
    "extract_pages",
    "fixture_documents",
    "idempotency_key",
    "implements",
    "read_document",
]
