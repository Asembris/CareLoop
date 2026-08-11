"""Disk cache for model responses.

Two jobs. Re-running an unchanged task set must cost nothing, and the committed cache must let
someone else reproduce the results table without any credentials at all.

The key covers everything that could change an answer — candidate, model id, endpoint, prompt,
system prompt, schema and sampling parameters. It deliberately does **not** cover the expected
answer or the scoring code: changing what counts as correct must re-score the same responses
rather than re-buy them.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

CACHE_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "cache"


class CachedCall(BaseModel):
    """One recorded model call. Committed to the repo as data."""

    model_config = ConfigDict(frozen=True)

    key: str
    candidate_id: str
    task_id: str
    model_id: str

    ok: bool
    """Whether a schema-valid instance came back. False records a failure as data."""

    output: dict[str, Any] | None = None
    error_type: str = ""
    error_message: str = ""

    input_tokens: int = 0
    output_tokens: int = 0

    tokens_estimated: bool = False
    """True when the provider reported no usage and the counts are a character-based estimate.

    ``strands-agents==1.51.0`` surfaces usage through Bedrock's ``structured_output`` but not
    through the OpenAI-compatible one, which calls ``beta.chat.completions.parse`` and discards
    ``response.usage``. Recording which is which keeps a measured cost and a guessed one from
    being read off the same column.
    """

    latency_ms: float = 0.0
    """Wall-clock, measured here. What a caregiver would actually wait."""

    provider_latency_ms: float = 0.0
    """Provider-reported, excluding network. Recorded to separate model time from link time."""


class CacheFile(BaseModel):
    """Every recorded call for one candidate."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    calls: dict[str, CachedCall] = Field(default_factory=dict)


def call_key(
    *,
    candidate_id: str,
    model_id: str,
    base_url: str,
    task_id: str,
    system_prompt: str,
    prompt: str,
    schema_name: str,
    schema_fields: tuple[str, ...],
    params: dict[str, Any],
) -> str:
    """A stable digest over everything that determines the answer.

    ``json.dumps`` with ``sort_keys`` for the same reason ``tools/idempotency.py`` uses it: quoting
    makes the encoding injective, where a delimiter-joined form lets two different inputs collide.
    """
    payload = {
        "candidate_id": candidate_id,
        "model_id": model_id,
        "base_url": base_url,
        "task_id": task_id,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "schema_name": schema_name,
        "schema_fields": list(schema_fields),
        "params": params,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResponseCache:
    """Per-candidate JSON files under :data:`CACHE_DIR`."""

    def __init__(self, directory: pathlib.Path = CACHE_DIR) -> None:
        self.directory = directory
        self._loaded: dict[str, CacheFile] = {}

    def path_for(self, candidate_id: str) -> pathlib.Path:
        return self.directory / f"{candidate_id}.json"

    def _file(self, candidate_id: str) -> CacheFile:
        if candidate_id not in self._loaded:
            path = self.path_for(candidate_id)
            if path.exists():
                self._loaded[candidate_id] = CacheFile.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            else:
                self._loaded[candidate_id] = CacheFile(candidate_id=candidate_id)
        return self._loaded[candidate_id]

    def get(self, candidate_id: str, key: str) -> CachedCall | None:
        return self._file(candidate_id).calls.get(key)

    def put(self, call: CachedCall) -> None:
        current = self._file(call.candidate_id)
        merged = dict(current.calls)
        merged[call.key] = call
        self._loaded[call.candidate_id] = CacheFile(candidate_id=call.candidate_id, calls=merged)

    def flush(self) -> list[pathlib.Path]:
        """Write every loaded candidate file. Returns the paths written."""
        self.directory.mkdir(parents=True, exist_ok=True)
        written: list[pathlib.Path] = []
        for candidate_id, cache_file in sorted(self._loaded.items()):
            path = self.path_for(candidate_id)
            ordered = CacheFile(
                candidate_id=candidate_id,
                calls=dict(sorted(cache_file.calls.items())),
            )
            path.write_text(
                json.dumps(ordered.model_dump(), indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            written.append(path)
        return written


__all__ = ["CACHE_DIR", "CacheFile", "CachedCall", "ResponseCache", "call_key"]
