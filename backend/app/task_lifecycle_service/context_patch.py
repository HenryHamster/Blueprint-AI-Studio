"""Open-ended JSONB context patching for task lifecycle writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

Number = int | float


@dataclass(frozen=True)
class ContextPatch:
    """Patch operation for `Task.context`.

    This is an operation shape, not a schema for allowed context keys. Unknown
    keys are preserved and may be set/deleted/incremented just like known keys.
    """

    set: Mapping[str, Any] = field(default_factory=dict)
    delete: Sequence[str] = ()
    increment: Mapping[str, Number] = field(default_factory=dict)
    append_unique: Mapping[str, Sequence[Any]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.set or self.delete or self.increment or self.append_unique)


def apply_context_patch(
    context: Mapping[str, Any] | None,
    patch: ContextPatch,
) -> dict[str, Any]:
    """Return a patched copy of `context` without constraining key names."""

    next_context = dict(context or {})

    for key in patch.delete:
        next_context.pop(key, None)

    for key, delta in patch.increment.items():
        current = next_context.get(key, 0)
        if current is None:
            current = 0
        if not isinstance(current, int | float):
            raise TypeError(f"context key {key!r} cannot be incremented from {type(current).__name__}")
        next_context[key] = current + delta

    for key, values in patch.append_unique.items():
        current = next_context.get(key)
        if current is None:
            merged: list[Any] = []
        elif isinstance(current, list):
            merged = list(current)
        else:
            merged = [current]

        for value in values:
            if not any(existing == value for existing in merged):
                merged.append(value)
        next_context[key] = merged

    # Explicit sets win over delete/increment/append operations when a caller
    # provides the same key in more than one patch section.
    next_context.update(dict(patch.set))
    return next_context
