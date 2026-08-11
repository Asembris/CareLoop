"""Model providers.

``mock`` holds the scripted provider the test suite runs on. ``providers`` maps CareLoop's three
roles onto a real provider when one is configured, and onto the mock when one is not.
"""

from careloop.llm.mock import (
    MockModel,
    MockScriptExhausted,
    MockScriptTypeMismatch,
    RecordedCall,
    mock_model,
)
from careloop.llm.providers import (
    MalformedModelSpec,
    MissingCredential,
    ModelRole,
    ModelSpec,
    UnknownProvider,
    build_model,
    model_for,
    parse_spec,
    spec_for_role,
)

__all__ = [
    "MalformedModelSpec",
    "MissingCredential",
    "MockModel",
    "MockScriptExhausted",
    "MockScriptTypeMismatch",
    "ModelRole",
    "ModelSpec",
    "RecordedCall",
    "UnknownProvider",
    "build_model",
    "mock_model",
    "model_for",
    "parse_spec",
    "spec_for_role",
]
