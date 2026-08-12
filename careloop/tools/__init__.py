"""Tools: the registry that owns argument classes, the tool bodies, and the executor."""

from careloop.tools.callables import (
    ToolBindingError,
    ToolSet,
    UnknownTool,
    bound_method_name,
    bound_tools,
    implements,
)
from careloop.tools.documents import (
    FIXTURE_DOCUMENTS,
    POISONED_DOCUMENT_ID,
    DocumentNotFound,
    DocumentStore,
    default_document_store,
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
    "FIXTURE_DOCUMENTS",
    "POISONED_DOCUMENT_ID",
    "REGISTRY",
    "AccessGranted",
    "ActionResult",
    "AppointmentBooked",
    "ArgSpec",
    "Document",
    "DocumentNotFound",
    "DocumentStore",
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
    "idempotency_key",
    "implements",
]
