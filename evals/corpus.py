"""The committed attack corpus.

**An attack in this repository is data with provenance, or it is not an attack.** In October a
folder of adversarial strings with no record of where they came from is indistinguishable from a
folder of strings somebody wrote to pass their own test, and no amount of prose in the report
recovers the difference. So every record names the vulnerability class and attack technique it was
generated for, the simulator model that produced it, the DeepTeam version that was driving, and
the date. :class:`AttackRecord` refuses to load without them.

Two origins, and the distinction is enforced rather than described:

``generated``  emitted by a DeepTeam simulator during a paid run. Must name its simulator, the
               DeepTeam version and the date, because that is what makes it reproducible evidence
               rather than an assertion.

``authored``   written by hand. Must carry a ``rationale`` saying why. These exist because
               CareLoop has its own canonical threat model — the poisoned continuity notice the
               project was built around — and excluding it to keep the corpus tidy would mean the
               replay never covered the one attack the whole system was designed against. They
               are counted separately in the report, always, because an authored attack that
               fails is weaker evidence than a generated one that fails.

This module imports no ``deepteam``. It is read by the offline replay, which is what lets the CI
job that prints the headline number install ``.[dev]`` alone.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

#: Where committed attack files live. Every ``*.json`` here is loaded.
CORPUS_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "attacks"


class CorpusError(Exception):
    """Raised when the committed corpus cannot be loaded as stated."""


class AttackOrigin(StrEnum):
    """Where an attack payload came from."""

    GENERATED = "generated"
    AUTHORED = "authored"


class AttackRecord(BaseModel):
    """One adversarial payload, and the provenance that makes it evidence.

    Frozen, and every provenance field is required for the origin that needs it. A record that
    cannot say where it came from fails at load rather than being silently counted in a headline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attack_id: str
    payload: str

    vulnerability: str
    """DeepTeam vulnerability class, e.g. ``ExcessiveAgency``."""

    vulnerability_type: str
    """The specific type within it, e.g. ``PERMISSIONS``."""

    attack_technique: str
    """DeepTeam attack class, e.g. ``PromptInjection``. ``baseline`` for authored records."""

    origin: AttackOrigin

    simulator_model: str = ""
    deepteam_version: str = ""
    generated_at: str = ""
    """ISO date the payload was generated. A date, not a timestamp: it identifies the run."""

    rationale: str = ""
    """Why an authored payload is in the corpus. Required for authored records."""

    @model_validator(mode="after")
    def _provenance_matches_the_origin(self) -> AttackRecord:
        if self.origin is AttackOrigin.GENERATED:
            missing = [
                name
                for name in ("simulator_model", "deepteam_version", "generated_at")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    f"generated attack {self.attack_id!r} does not record {missing}; without it "
                    f"the corpus cannot be defended as generated rather than authored"
                )
        elif not self.rationale:
            raise ValueError(
                f"authored attack {self.attack_id!r} carries no rationale; an attack written by "
                f"hand has to say why it is in a corpus that otherwise proves itself"
            )
        if not self.payload.strip():
            raise ValueError(f"attack {self.attack_id!r} has an empty payload")
        return self


class Corpus(BaseModel):
    """Every committed attack, in a stable order."""

    model_config = ConfigDict(frozen=True)

    records: tuple[AttackRecord, ...]

    def __iter__(self) -> Iterator[AttackRecord]:  # type: ignore[override]
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def of_origin(self, origin: AttackOrigin) -> tuple[AttackRecord, ...]:
        return tuple(record for record in self.records if record.origin is origin)

    @property
    def vulnerabilities(self) -> tuple[str, ...]:
        return tuple(sorted({record.vulnerability for record in self.records}))

    @property
    def techniques(self) -> tuple[str, ...]:
        return tuple(sorted({record.attack_technique for record in self.records}))


def load_corpus(directory: pathlib.Path | None = None) -> Corpus:
    """Load every committed attack file.

    Read in filename order, then sorted by ``attack_id``, so two runs replay the same attacks in
    the same sequence and a results file diffs cleanly.

    Raises:
        CorpusError: if a file is unreadable, an id is declared twice, or the corpus is empty. An
            empty corpus silently replaying nothing would report ``0 of 0`` and look like a pass.
    """
    root = directory if directory is not None else CORPUS_DIR
    if not root.is_dir():
        raise CorpusError(f"no attack corpus directory at {root}")

    by_id: dict[str, AttackRecord] = {}
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as broken:
            raise CorpusError(f"{path.name} is not valid JSON: {broken}") from broken

        entries = payload.get("attacks") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise CorpusError(f"{path.name} holds no 'attacks' list")

        for entry in entries:
            record = AttackRecord.model_validate(entry)
            if record.attack_id in by_id:
                raise CorpusError(f"attack {record.attack_id!r} is declared twice")
            by_id[record.attack_id] = record

    if not by_id:
        raise CorpusError(
            f"no attacks found under {root}; a replay over an empty corpus reports 0 of 0, which "
            f"reads like a pass and proves nothing"
        )
    return Corpus(records=tuple(by_id[key] for key in sorted(by_id)))


def write_corpus(records: Iterable[AttackRecord], path: pathlib.Path) -> int:
    """Write generated attacks to ``path`` as committable data. Returns how many were written."""
    ordered = sorted(records, key=lambda record: record.attack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"attacks": [record.model_dump(mode="json") for record in ordered]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(ordered)


__all__ = [
    "CORPUS_DIR",
    "AttackOrigin",
    "AttackRecord",
    "Corpus",
    "CorpusError",
    "load_corpus",
    "write_corpus",
]
