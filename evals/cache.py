"""Disk cache for simulator and judge responses.

Two jobs, the same two ``bench/cache.py`` has: re-running an unchanged suite must cost nothing,
and the committed cache must let somebody else reproduce the results table with no credentials at
all. Written separately rather than shared because the unit differs — the benchmark caches one
task's answer, this caches one ``a_generate`` call — and a shared key covering both would have to
be vague enough to cover neither well.

**The key covers everything that could change an answer** and deliberately nothing else: role,
model id, prompt, schema name, the schema as transmitted, and the token budget. It does not cover
the vulnerability the attack was generated for or how the result is scored, because changing what
counts as a finding must re-score the same responses rather than re-buy them.

The **schema as transmitted** is in the key rather than the schema's name, and that is a
consequence of ``docs/spike-findings.md`` §7.6: a bare ``model_json_schema()`` and its
strict-compliant form are the same class asking two different questions, and NVIDIA answers them
differently. Keying on the name alone would serve an unconstrained answer to a constrained
request.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Callable
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

CACHE_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "cache"


class CachedResponse(BaseModel):
    """One recorded provider call. Committed to the repository as data."""

    model_config = ConfigDict(frozen=True)

    key: str
    role: str
    model_id: str
    schema_name: str

    ok: bool
    """Whether a schema-valid instance came back. ``False`` records a failure as data rather than
    losing it, so a rerun does not re-buy a call that is going to fail the same way."""

    output: dict[str, Any] | None = None
    text: str = ""
    """Populated instead of ``output`` when the call was made without a schema."""

    error_type: str = ""
    error_message: str = ""


def response_key(fields: tuple[str, ...]) -> str:
    """A stable digest over everything that could change the answer."""
    encoded = json.dumps(list(fields), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResponseCache:
    """Reads committed responses, and collects new ones for writing.

    New entries are held in memory until :meth:`flush`, so a run that aborts on a ceiling does not
    leave a half-written cache that a later run would treat as authoritative.
    """

    def __init__(self, directory: pathlib.Path | None = None, *, enabled: bool = True) -> None:
        self.directory = directory if directory is not None else CACHE_DIR
        self.enabled = enabled
        self._pending: dict[str, CachedResponse] = {}
        self._hits = 0
        self._misses = 0

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def path_for(self, key: str) -> pathlib.Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        if not self.enabled:
            return None
        if key in self._pending:
            return self._pending[key]
        path = self.path_for(key)
        if not path.exists():
            return None
        return CachedResponse.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, response: CachedResponse) -> None:
        self._pending[response.key] = response

    def flush(self) -> int:
        """Write pending entries. Returns how many files were written."""
        if not self._pending:
            return 0
        self.directory.mkdir(parents=True, exist_ok=True)
        for key, response in self._pending.items():
            self.path_for(key).write_text(
                json.dumps(response.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        written = len(self._pending)
        self._pending.clear()
        return written

    def resolve(
        self,
        adapter: Any,
        prompt: str,
        schema: type[BaseModel] | None,
        call: Callable[[str, type[BaseModel] | None], Any],
    ) -> Any:
        """Return a cached answer, or make the call and record it.

        Failures are cached too, and re-raised on a hit. A run that re-buys a call which failed
        deterministically last time is a run whose ceiling protects nothing.
        """
        from evals.models import strict_schema

        schema_name = schema.__name__ if schema is not None else ""
        schema_json = (
            json.dumps(strict_schema(schema), sort_keys=True) if schema is not None else ""
        )
        key = response_key(
            (
                str(adapter.role),
                adapter.get_model_name(),
                prompt,
                schema_name,
                schema_json,
                str(adapter.max_tokens),
            )
        )

        cached = self.get(key)
        if cached is not None:
            self._hits += 1
            if not cached.ok:
                raise RuntimeError(
                    f"cached failure for {cached.model_id} ({cached.error_type}): "
                    f"{cached.error_message}"
                )
            if schema is None:
                return cached.text
            return schema.model_validate(cached.output)

        self._misses += 1
        try:
            result = call(prompt, schema)
        except Exception as failure:
            self.put(
                CachedResponse(
                    key=key,
                    role=str(adapter.role),
                    model_id=adapter.get_model_name(),
                    schema_name=schema_name,
                    ok=False,
                    error_type=type(failure).__name__,
                    error_message=str(failure)[:500],
                )
            )
            raise

        self.put(
            CachedResponse(
                key=key,
                role=str(adapter.role),
                model_id=adapter.get_model_name(),
                schema_name=schema_name,
                ok=True,
                output=result.model_dump(mode="json") if schema is not None else None,
                text="" if schema is not None else str(result),
            )
        )
        return result


__all__ = ["CACHE_DIR", "CachedResponse", "ResponseCache", "response_key"]
