"""AST inventory helpers for backend websocket emitter call sites."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any


BROADCAST_CALLEES = {
    "broadcast",
    "broadcast_to_all",
    "broadcast_to_project",
}
HELPER_CALLEES = {
    "emit_workbench_refetch",
    "emit_strategic_cycle_completed",
    "emit_strategic_cycle_started",
    "emit_strategic_updated",
}
SEND_CALLEES = {"send_json"}
WATCHED_CALLEES = BROADCAST_CALLEES | HELPER_CALLEES | SEND_CALLEES
IMPORT_MODULES = {
    "app.broadcast",
    "app.workbench_events",
    "app.strategic_events",
}
STRATEGIC_HELPER_EVENTS = {
    "emit_strategic_cycle_started": "strategic_cycle_started",
    "emit_strategic_cycle_completed": "strategic_cycle_completed",
    "emit_strategic_updated": "strategic_updated",
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        current: ast.AST = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ast.unparse(func)


def _call_leaf(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _expression(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _dict_value(node: ast.AST | None, key_name: str) -> ast.AST | None:
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values, strict=False):
        if _literal_string(key) == key_name:
            return value
    return None


def _payload_arg_kind(node: ast.AST | None) -> str:
    if node is None:
        return "none"
    if isinstance(node, ast.Dict):
        keys = [
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        if keys:
            return "dict:" + ",".join(sorted(keys))
        return "dict"
    if isinstance(node, ast.Name):
        return f"name:{node.id}"
    if isinstance(node, ast.Call):
        return f"call:{_call_name(node)}"
    if isinstance(node, ast.Constant):
        return f"constant:{type(node.value).__name__}"
    if isinstance(node, ast.Attribute):
        return f"attribute:{_expression(node)}"
    return type(node).__name__


def _enclosing_scope(stack: list[ast.AST]) -> str:
    parts: list[str] = []
    for node in stack:
        if isinstance(node, ast.ClassDef):
            parts.append(node.name)
        elif isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            parts.append(node.name)
    return ".".join(parts) or "<module>"


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in IMPORT_MODULES:
            continue
        for alias in node.names:
            if alias.name in WATCHED_CALLEES:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _canonical_callee(raw_name: str, aliases: dict[str, str]) -> str | None:
    leaf = _call_leaf(raw_name)
    if raw_name in aliases:
        return aliases[raw_name]
    if leaf in WATCHED_CALLEES:
        return leaf
    return None


def _event_fields(
    node: ast.Call,
    canonical_callee: str,
) -> tuple[str, bool, str | None, ast.AST | None]:
    event_node: ast.AST | None = None
    if canonical_callee in {"broadcast", "broadcast_to_project"}:
        event_node = node.args[1] if len(node.args) > 1 else None
    elif canonical_callee == "broadcast_to_all":
        event_node = node.args[0] if node.args else None
    elif canonical_callee == "send_json":
        event_node = _dict_value(node.args[0] if node.args else None, "event")
    elif canonical_callee == "emit_workbench_refetch":
        event_node = _keyword_value(node, "event_name")
        if event_node is None:
            return "workbench_refetch", True, None, None
    elif canonical_callee in STRATEGIC_HELPER_EVENTS:
        return STRATEGIC_HELPER_EVENTS[canonical_callee], True, None, None

    literal = _literal_string(event_node)
    if literal is not None:
        return literal, True, None, event_node
    return "<dynamic>", False, _expression(event_node), event_node


def _payload_arg(node: ast.Call, canonical_callee: str) -> ast.AST | None:
    if canonical_callee in {"broadcast", "broadcast_to_project"}:
        return node.args[2] if len(node.args) > 2 else None
    if canonical_callee == "broadcast_to_all":
        return node.args[1] if len(node.args) > 1 else None
    if canonical_callee == "send_json":
        return node.args[0] if node.args else None
    return None


def _literal_kw(node: ast.Call, name: str) -> str | None:
    return _literal_string(_keyword_value(node, name))


def _classification(
    *,
    source_path: str,
    canonical_callee: str,
    callee: str,
    event: str,
    payload_arg_kind: str,
) -> str:
    if source_path in {"backend/app/broadcast.py", "backend/app/event_fanout.py"}:
        return "transport"
    if canonical_callee == "emit_workbench_refetch":
        return "workbench_v1_helper"
    if canonical_callee in STRATEGIC_HELPER_EVENTS:
        return "strategic_v1_helper"
    if source_path == "backend/app/workbench_events.py" and canonical_callee == "broadcast_to_project":
        return "workbench_v1_emitter"
    if source_path == "backend/app/strategic_events.py" and canonical_callee == "broadcast_to_project":
        return "strategic_v1_emitter"
    if (
        event in {"message_created", "message_updated"}
        or "message_event" in payload_arg_kind
        or "message_event_payload" in payload_arg_kind
    ):
        return "message_v1_payload"
    if canonical_callee == "send_json":
        return "raw_websocket_control"
    if canonical_callee == "broadcast_to_all" or callee.endswith(".broadcast_to_all"):
        return "legacy_global_event"
    if event in {
        "alert_dismissed",
        "anomaly_adjustment",
        "autonomous_activity",
        "autonomous_paused",
        "autonomy_updated",
        "project_updated",
    }:
        return "legacy_review_required"
    return "legacy_project_event"


def _inventory_record(
    *,
    source_path: str,
    stack: list[ast.AST],
    node: ast.Call,
    callee: str,
    canonical_callee: str,
) -> dict[str, Any]:
    event, event_is_literal, event_expression, _event_node = _event_fields(
        node,
        canonical_callee,
    )
    payload = _payload_arg(node, canonical_callee)
    payload_arg_kind = _payload_arg_kind(payload)
    record = {
        "source_path": source_path,
        "scope": _enclosing_scope(stack),
        "lineno": node.lineno,
        "callee": callee,
        "canonical_callee": canonical_callee,
        "event": event,
        "event_is_literal": event_is_literal,
        "event_expression": event_expression,
        "payload_arg_kind": payload_arg_kind,
        "workbench_resource": None,
        "workbench_action": None,
        "classification": _classification(
            source_path=source_path,
            canonical_callee=canonical_callee,
            callee=callee,
            event=event,
            payload_arg_kind=payload_arg_kind,
        ),
        "notes": "",
    }
    if canonical_callee == "emit_workbench_refetch":
        record["workbench_resource"] = _literal_kw(node, "resource")
        record["workbench_action"] = _literal_kw(node, "action")
    return record


def build_websocket_event_inventory(app_dir: Path) -> list[dict[str, Any]]:
    app_dir = app_dir.resolve()
    backend_root = app_dir.parent
    records: list[dict[str, Any]] = []
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source_path = f"backend/{path.relative_to(backend_root).as_posix()}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        stack: list[ast.AST] = []

        class Visitor(ast.NodeVisitor):
            def generic_visit(self, node: ast.AST) -> None:
                stack.append(node)
                super().generic_visit(node)
                stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                callee = _call_name(node)
                canonical_callee = _canonical_callee(callee, aliases)
                if canonical_callee is not None:
                    records.append(
                        _inventory_record(
                            source_path=source_path,
                            stack=stack,
                            node=node,
                            callee=callee,
                            canonical_callee=canonical_callee,
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)

    occurrences: defaultdict[tuple[str, ...], int] = defaultdict(int)
    for record in records:
        occurrence_key = (
            str(record["source_path"]),
            str(record["scope"]),
            str(record["callee"]),
            str(record["event"]),
            str(record["event_expression"]),
            str(record["payload_arg_kind"]),
        )
        occurrences[occurrence_key] += 1
        record["occurrence"] = occurrences[occurrence_key]
    return records


def inventory_signature(record: dict[str, Any]) -> dict[str, Any]:
    """Return the stable snapshot comparison shape.

    ``lineno`` is kept in the checked-in snapshot for reviewer navigation, but
    ignored for drift detection so unrelated edits do not churn the inventory.
    """

    return {key: value for key, value in record.items() if key != "lineno"}
