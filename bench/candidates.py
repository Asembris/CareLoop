"""The models under test.

Specs are written in the same vocabulary ``careloop.llm.providers`` parses, so a candidate that
wins here is configured by pasting its spec into ``.env``. A separate candidate vocabulary would
allow a model to be benchmarked in a shape CareLoop cannot actually be pointed at.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from bench.tasks import TaskKind


class Candidate(BaseModel):
    """One model under test."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    """Short stable name. Used as the cache filename, so it must not change casually."""

    spec: str
    """A spec string for ``careloop.llm.providers.parse_spec``."""

    label: str
    kinds: tuple[TaskKind, ...] = ("triage", "planner", "extraction")
    """Which task kinds this candidate is measured on."""

    note: str = ""


CANDIDATES: Final[tuple[Candidate, ...]] = (
    Candidate(
        candidate_id="nova-lite",
        spec="bedrock:us.amazon.nova-lite-v1:0",
        label="Nova Lite (us profile)",
        note="The plan's intended primary. Cheapest of the Bedrock text candidates bar Micro.",
    ),
    Candidate(
        candidate_id="nova-2-lite-us",
        spec="bedrock:us.amazon.nova-2-lite-v1:0",
        label="Nova 2 Lite (us profile)",
        note="Newer generation. 5x the input and ~10x the output price of Nova Lite.",
    ),
    Candidate(
        candidate_id="nova-2-lite-global",
        spec="bedrock:global.amazon.nova-2-lite-v1:0",
        label="Nova 2 Lite (global profile)",
        note=(
            "Same model, global inference profile. Included to see whether the profile changes "
            "latency or availability before anything is deployed to AgentCore."
        ),
    ),
    Candidate(
        candidate_id="nova-micro",
        spec="bedrock:us.amazon.nova-micro-v1:0",
        label="Nova Micro (triage only)",
        kinds=("triage",),
        note="Text-only and cheapest. Measured on classification alone, per the plan.",
    ),
    Candidate(
        candidate_id="gpt-4o-mini",
        spec="openai:gpt-4o-mini",
        label="OpenAI gpt-4o-mini",
        note="Paid from a small non-replaceable balance. Subject to the spend guard.",
    ),
    Candidate(
        candidate_id="nvidia-llama-3.3-70b",
        spec="nvidia:meta/llama-3.3-70b-instruct",
        label="NVIDIA NIM llama-3.3-70b-instruct",
        note="The OpenAI-compatible path against a third endpoint.",
    ),
)

BY_ID: Final[dict[str, Candidate]] = {c.candidate_id: c for c in CANDIDATES}


def resolve(names: list[str] | None) -> tuple[Candidate, ...]:
    """Select candidates by id, or all of them when ``names`` is empty."""
    if not names:
        return CANDIDATES
    unknown = [name for name in names if name not in BY_ID]
    if unknown:
        raise KeyError(f"unknown candidate(s) {unknown}; known: {sorted(BY_ID)}")
    return tuple(BY_ID[name] for name in names)


__all__ = ["BY_ID", "CANDIDATES", "Candidate", "resolve"]
