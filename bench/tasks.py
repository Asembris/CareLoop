"""The benchmark task set.

Tasks and their expected answers live in JSON under ``bench/tasks/``, never in harness code. The
task set has to be able to grow without touching logic — otherwise every new task is a code change
and the temptation to tweak the scoring to fit becomes real.

A task may reference a fixture document by id. The harness inlines that document's text into the
prompt, so the document content is also not duplicated into the task file: the poisoned fixture in
particular must be exactly the one the rest of the suite tests against, not a copy that could be
quietly defanged.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bench.schemas import ARG_NAMES, SCHEMA_FOR_KIND, TOOL_NAMES

TASKS_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "tasks"

TaskKind = Literal["triage", "planner", "extraction"]


class Expectation(BaseModel):
    """The fixed right answer for one task."""

    model_config = ConfigDict(frozen=True)

    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Expected values for named output fields. Compared exactly.",
    )
    tool_name: str = ""
    """For planner tasks: the tool that should be proposed."""

    args: dict[str, str] = Field(
        default_factory=dict,
        description="For planner tasks: argument values that must appear on the intent.",
    )


class InjectionProbe(BaseModel):
    """Values that appear only in attacker-controlled document content.

    A model that puts one of these into its intent has been steered by the document. That is
    recorded as **injection susceptibility**, a metric of its own — deliberately not folded into
    tool-selection correctness, because the policy engine denying this action is the system
    working as designed. The model being steerable and the system being safe are different claims
    and must not share a number.
    """

    model_config = ConfigDict(frozen=True)

    forbidden_values: tuple[str, ...] = ()
    """Substrings that must not appear in any proposed argument."""

    note: str = ""


class Task(BaseModel):
    """One scripted benchmark task."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    kind: TaskKind
    prompt: str
    document_id: str = ""
    """Optional fixture document whose text is inlined into the prompt."""

    expect: Expectation = Field(default_factory=Expectation)
    injection: InjectionProbe | None = None

    @model_validator(mode="after")
    def _check_expectation_matches_kind(self) -> Task:
        if self.kind == "planner":
            if not self.expect.tool_name:
                raise ValueError(f"planner task {self.task_id!r} declares no expected tool")
            if self.expect.tool_name not in TOOL_NAMES:
                raise ValueError(
                    f"planner task {self.task_id!r} expects tool {self.expect.tool_name!r}, "
                    f"which the registry does not declare"
                )
            unknown = set(self.expect.args) - set(ARG_NAMES)
            if unknown:
                raise ValueError(
                    f"planner task {self.task_id!r} expects arguments {sorted(unknown)}, "
                    f"which no registered tool declares"
                )
        else:
            if self.expect.tool_name or self.expect.args:
                raise ValueError(
                    f"{self.kind} task {self.task_id!r} declares a tool expectation; "
                    f"only planner tasks may"
                )
            if not self.expect.fields:
                raise ValueError(f"{self.kind} task {self.task_id!r} expects no fields")

        schema = SCHEMA_FOR_KIND[self.kind]
        unknown_fields = set(self.expect.fields) - set(schema.model_fields)
        if unknown_fields:
            raise ValueError(
                f"task {self.task_id!r} expects fields {sorted(unknown_fields)}, which "
                f"{schema.__name__} does not declare"
            )
        return self

    @property
    def output_model(self) -> type[BaseModel]:
        """The schema this task's answer must validate against."""
        return SCHEMA_FOR_KIND[self.kind]


class TaskSet(BaseModel):
    """An ordered, id-unique collection of tasks."""

    model_config = ConfigDict(frozen=True)

    tasks: tuple[Task, ...]

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> TaskSet:
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                raise ValueError(f"task id {task.task_id!r} appears twice")
            seen.add(task.task_id)
        return self

    def __iter__(self):  # type: ignore[override]
        return iter(self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)

    def of_kind(self, kind: TaskKind) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks if task.kind == kind)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(task.kind for task in self.tasks)


def load_task_files(paths: Iterable[pathlib.Path]) -> TaskSet:
    """Load and validate tasks from explicit JSON files."""
    collected: list[Task] = []
    for path in sorted(paths):
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        entries = payload["tasks"] if isinstance(payload, dict) else payload
        for entry in entries:
            collected.append(Task.model_validate(entry))
    return TaskSet(tasks=tuple(collected))


def load_tasks(directory: pathlib.Path = TASKS_DIR) -> TaskSet:
    """Load every ``*.json`` task file in ``directory``."""
    return load_task_files(directory.glob("*.json"))


__all__ = [
    "TASKS_DIR",
    "Expectation",
    "InjectionProbe",
    "Task",
    "TaskKind",
    "TaskSet",
    "load_task_files",
    "load_tasks",
]
