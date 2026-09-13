"""Deterministic bounded model context for one frozen Run."""

import json

from backend.db import ResourceNotFound, _json


class ContextTooLarge(ValueError):
    code = "context_too_large"


def _model_message(row):
    message = {"role": row["role"], "content": row["content"]}
    if row["tool_calls_json"]:
        message["tool_calls"] = json.loads(row["tool_calls_json"])
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    return message


def _complete_groups(rows):
    groups = []
    index = 0
    while index < len(rows):
        row = rows[index]
        if row["role"] == "tool":
            index += 1
            continue
        message = _model_message(row)
        calls = message.get("tool_calls") or []
        if row["role"] != "assistant" or not calls:
            groups.append([message])
            index += 1
            continue
        ids = {call.get("id") for call in calls}
        group = [message]
        index += 1
        seen = set()
        while index < len(rows) and rows[index]["role"] == "tool":
            tool = _model_message(rows[index])
            if tool.get("tool_call_id") not in ids or len(tool.get("content") or "") > 8000:
                break
            group.append(tool)
            seen.add(tool["tool_call_id"])
            index += 1
        if seen == ids:
            groups.append(group)
    return groups


def build_context(database, run_id, system_prompt="", max_messages=128, max_chars=64000):
    """Exclude every task-only input; add this Run's input once after frozen history."""
    if not 1 <= max_messages <= 128 or not 1 <= max_chars <= 64000:
        raise ValueError("Context 上限只能下调")
    run = database.get_run(run_id)
    if not run:
        raise ResourceNotFound("Run 不存在")
    task = database.get_task(run["task_id"])
    if not task:
        raise ResourceNotFound("Task 不存在")
    input_row = database._query_one("""
        SELECT * FROM messages WHERE task_id=? AND run_id IS NULL
    """, (task["id"],))
    if not input_row:
        raise RuntimeError("Task 输入消息缺失")
    prefix = []
    if system_prompt:
        prefix.append({"role": "system", "content": system_prompt})
    prefix.append({"role": "system", "content": _json({
        "goal": task["goal"],
        "acceptance": json.loads(task["acceptance_json"]),
        "context": json.loads(task["context_json"]),
    })})
    current = _model_message(input_row)
    # Tool schemas are sent separately to the model but count against this budget.
    snapshot_chars = len(run["tool_snapshot_json"]) + len(run["config_snapshot_json"])
    history_rows = database._query_all("""
        SELECT * FROM messages WHERE session_id=?
            AND sequence<=?
            AND (task_id IS NULL OR run_id IS NOT NULL)
        ORDER BY sequence
    """, (run["session_id"], run["context_max_sequence"]))
    own_rows = database._query_all("""
        SELECT * FROM messages WHERE run_id=? AND sequence>?
        ORDER BY sequence
    """, (run_id, run["context_max_sequence"]))
    own = [message for group in _complete_groups(own_rows) for message in group]
    core = [*prefix, current, *own]
    if len(core) > max_messages or len(_json(core)) + snapshot_chars > max_chars:
        raise ContextTooLarge("系统策略或当前 Run 消息超过 Context 上限")
    chosen = []
    for group in reversed(_complete_groups(history_rows)):
        candidate = [*prefix, *group, *chosen, current, *own]
        if len(candidate) > max_messages or len(_json(candidate)) + snapshot_chars > max_chars:
            break
        chosen = [*group, *chosen]
    return [*prefix, *chosen, current, *own]
