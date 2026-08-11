"""Tools: the registry that owns argument classes, and later the executor."""

from careloop.tools.documents import (
    FIXTURE_DOCUMENTS,
    POISONED_DOCUMENT_ID,
    DocumentNotFound,
    DocumentStore,
    default_document_store,
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
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UncanonicalizableArgument",
    "args_digest",
    "canonical_args",
    "default_document_store",
    "idempotency_key",
]
