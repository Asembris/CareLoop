"""PROVISIONAL output schemas for the benchmark.

**These are not CareLoop's agent schemas.** ``TriageResult`` and ``PlannerIntent`` are commit 5's
deliverables and belong in ``careloop/agents/``. The versions here exist so this session can
measure structured-output success before those schemas are designed, and commit 5 should design
them on the merits rather than inherit whatever the benchmark found convenient.

What the real schemas *should* take from this file is the measurement, not the shape.

Two deliberate constraints, both about not letting the harness cause failures it then blames on
the model:

**Every field is required and typed ``str``.** Optional fields and ``X | None`` unions render as
nullable JSON-schema types that some strict structured-output implementations reject. A rejection
would show up as a structured-output failure — the benchmark's disqualifying metric — when the
real cause was the harness. The empty string means "not supplied", which the prompts state.

**``PlannerIntent`` is generated from the tool registry**, so its tool names and argument fields
cannot drift from the tools the policy engine actually governs. A benchmark measuring the model
against a stale tool list measures nothing.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field, create_model

from careloop.tools.registry import REGISTRY

#: Every tool name the registry declares, sorted for a stable schema.
TOOL_NAMES: Final[tuple[str, ...]] = tuple(sorted(REGISTRY.names))

#: The union of every argument name across every tool, sorted.
ARG_NAMES: Final[tuple[str, ...]] = tuple(
    sorted({arg for tool in REGISTRY for arg in tool.arg_names})
)

NOT_SUPPLIED: Final[str] = ""


class TriageResult(BaseModel):
    """PROVISIONAL. Classification of an inbound event into typed case fields."""

    category: Literal[
        "APPOINTMENT",
        "INSURANCE",
        "PRESCRIPTION",
        "RECORDS",
        "BILLING",
        "OTHER",
    ] = Field(description="What kind of administrative work this event represents.")
    urgency: Literal["ROUTINE", "SOON", "URGENT"] = Field(
        description=(
            "How quickly a caregiver must act. URGENT: today or tomorrow. "
            "SOON: within 14 days. ROUTINE: no deadline, or more than 14 days away. "
            "Judge by the stated deadline, not by tone."
        )
    )
    patient_id: str = Field(
        description="The patient identifier, exactly as written in the source. "
        "Empty string if the source does not state one."
    )
    summary: str = Field(description="One sentence describing what needs doing.")


class ExtractionResult(BaseModel):
    """PROVISIONAL. Field values pulled out of document text.

    Every field is a verbatim copy from the source, or the empty string when the source does not
    state it. Verbatim matters: the policy engine matches facts by exact canonical value, so a
    helpfully reformatted date is a value the engine will not recognise.
    """

    patient_name: str = Field(description="Patient's full name, verbatim, or empty string.")
    patient_id: str = Field(description="Patient identifier, verbatim, or empty string.")
    date_of_birth: str = Field(description="Date of birth, verbatim, or empty string.")
    referring_clinician: str = Field(description="Referring clinician, verbatim, or empty string.")
    referral_reason: str = Field(description="Clinical reason for referral, or empty string.")
    policy_number: str = Field(description="Insurance policy number, verbatim, or empty string.")
    member_reference: str = Field(description="Insurer reference number, or empty string.")
    provider_phone: str = Field(description="Contact telephone number, or empty string.")


def _build_planner_intent() -> type[BaseModel]:
    """Generate ``PlannerIntent`` from the registry so it cannot drift from the real tools."""
    fields: dict[str, tuple[type, object]] = {
        "tool_name": (
            Literal[TOOL_NAMES],  # type: ignore[valid-type]
            Field(description="Exactly one tool from the catalogue. Nothing else is callable."),
        ),
        "rationale": (
            str,
            Field(description="One sentence on why this action is the right next step."),
        ),
    }
    for arg in ARG_NAMES:
        owners = sorted(tool.name for tool in REGISTRY if arg in tool.arg_names)
        fields[arg] = (
            str,
            Field(
                description=(
                    f"Argument '{arg}' of {', '.join(owners)}. "
                    f"Empty string when calling a tool that does not take it."
                )
            ),
        )
    return create_model(
        "PlannerIntent",
        __doc__=(
            "PROVISIONAL. One proposed next action as a typed tool intent.\n\n"
            "Generated from careloop.tools.registry.REGISTRY: the tool names and argument fields "
            "are exactly the ones the policy engine governs."
        ),
        **fields,  # type: ignore[arg-type]
    )


PlannerIntent = _build_planner_intent()


def intent_args(intent: BaseModel) -> dict[str, str]:
    """The non-empty arguments of a :data:`PlannerIntent`, as a plain mapping.

    Empty strings are dropped: the flat schema carries every argument of every tool, so a call to
    ``fetch_document`` still has a ``to`` field, and treating that as a supplied recipient would
    make every intent look like it addressed an email.
    """
    dumped = intent.model_dump()
    return {name: dumped[name] for name in ARG_NAMES if dumped.get(name, NOT_SUPPLIED).strip()}


SCHEMA_FOR_KIND: Final[dict[str, type[BaseModel]]] = {
    "triage": TriageResult,
    "planner": PlannerIntent,
    "extraction": ExtractionResult,
}


__all__ = [
    "ARG_NAMES",
    "NOT_SUPPLIED",
    "SCHEMA_FOR_KIND",
    "TOOL_NAMES",
    "ExtractionResult",
    "PlannerIntent",
    "TriageResult",
    "intent_args",
]
