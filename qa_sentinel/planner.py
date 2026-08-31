"""Validate suite workflow dependencies and produce a stable DAG plan."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from .models import TestCase, TestSuite


class PlanError(ValueError):
    """Raised when a suite's workflow graph is not executable."""


@dataclass(frozen=True)
class PlanLayer:
    """One stable topological layer of ready step identifiers."""

    index: int
    step_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    """A declaration-ordered suite plus its stable topological layers."""

    suite_name: str
    schema_version: int
    steps: tuple[TestCase, ...]
    layers: tuple[PlanLayer, ...]

    @property
    def by_id(self) -> Mapping[str, TestCase]:
        return {step.case_id: step for step in self.steps}

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def has_dependencies(self) -> bool:
        return any(step.depends_on for step in self.steps)

    def layer_for(self, step_id: str) -> int:
        for layer in self.layers:
            if step_id in layer.step_ids:
                return layer.index
        raise KeyError(step_id)


_STEP_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*steps\.([A-Za-z][A-Za-z0-9._-]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)


def _references(value: object) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.extend(_STEP_REFERENCE_PATTERN.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_references(item))
    return tuple(found)


def _validate_references(steps: tuple[TestCase, ...], by_id: Mapping[str, TestCase]) -> None:
    for step in steps:
        values: tuple[object, ...] = (step.url, step.headers, step.body)
        for value in values:
            for target, capture_name in _references(value):
                if target not in by_id:
                    raise PlanError(
                        f"step '{step.case_id}' references unknown step '{target}'"
                    )
                if target not in step.depends_on:
                    raise PlanError(
                        f"step '{step.case_id}' references '{target}.{capture_name}' "
                        "without declaring it in depends_on"
                    )
                target_step = by_id[target]
                if capture_name not in target_step.extract:
                    raise PlanError(
                        f"step '{step.case_id}' references unknown capture "
                        f"'{target}.{capture_name}'"
                    )


def _validate_step_shape(step: TestCase) -> None:
    if not step.case_id.strip():
        raise PlanError("workflow step id cannot be empty")
    if step.run_if not in {"success", "always"}:
        raise PlanError(
            f"step '{step.case_id}' has unsupported run_if '{step.run_if}'; "
            "expected 'success' or 'always'"
        )
    if len(set(step.depends_on)) != len(step.depends_on):
        raise PlanError(f"step '{step.case_id}' contains duplicate dependencies")


def plan_suite(suite: TestSuite) -> ExecutionPlan:
    """Validate dependencies and return stable declaration-order layers.

    A layer contains all currently-ready steps in suite declaration order.
    The planner never sends a request and does not resolve captures; those are
    runtime concerns for later workflow batches.
    """

    steps = tuple(suite.tests)
    ids = [step.case_id for step in steps]
    if len(set(ids)) != len(ids):
        duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
        raise PlanError("duplicate workflow step id(s): " + ", ".join(duplicates))
    known = set(ids)
    by_id = {step.case_id: step for step in steps}
    for step in steps:
        _validate_step_shape(step)
        unknown = sorted(set(step.depends_on) - known)
        if unknown:
            raise PlanError(
                f"step '{step.case_id}' depends on unknown step(s): "
                + ", ".join(unknown)
            )
        if step.case_id in step.depends_on:
            raise PlanError(f"step '{step.case_id}' cannot depend on itself")
    _validate_references(steps, by_id)

    remaining = set(ids)
    layers: list[PlanLayer] = []
    layer_index = 0
    while remaining:
        ready = tuple(
            step.case_id
            for step in steps
            if step.case_id in remaining
            and all(dependency not in remaining for dependency in step.depends_on)
        )
        if not ready:
            cycle_ids = tuple(step.case_id for step in steps if step.case_id in remaining)
            raise PlanError(
                "workflow dependency cycle detected among step(s): "
                + ", ".join(cycle_ids)
            )
        layers.append(PlanLayer(layer_index, ready))
        remaining.difference_update(ready)
        layer_index += 1

    return ExecutionPlan(
        suite_name=suite.name,
        schema_version=suite.schema_version,
        steps=steps,
        layers=tuple(layers),
    )


def format_plan(plan: ExecutionPlan) -> str:
    """Render a compact, deterministic plan for CLI/dry-run consumers."""

    lines = [
        f"{plan.suite_name}: schema v{plan.schema_version}; "
        f"{plan.step_count} step(s); {len(plan.layers)} layer(s)"
    ]
    for layer in plan.layers:
        lines.append(f"  layer {layer.index}: " + ", ".join(layer.step_ids))
    return "\n".join(lines)
