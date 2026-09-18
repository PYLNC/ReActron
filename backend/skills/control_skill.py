"""P2 内部控制工具的固定模型参数；身份和权限只从 RunContext 注入。"""

_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_REFS = {"type": "array", "items": _ID, "maxItems": 16, "uniqueItems": True}


def _object(properties, required):
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


CONTROL_TOOLS = {
    "delegate_task": ("向已授权子 Agent 派发任务", _object({
        "assignee_agent_id": _ID,
        "goal": {"type": "string", "minLength": 1, "maxLength": 32768},
        "acceptance": {"type": "array", "items": {"type": "string", "minLength": 1,
                                                   "maxLength": 1000}, "maxItems": 16},
        "context": _object({"message_refs": _REFS, "artifact_refs": _REFS}, []),
        "limits": _object({"max_rounds": {"type": "integer", "minimum": 1, "maximum": 20},
                           "max_tool_calls": {"type": "integer", "minimum": 1,
                                              "maximum": 32}}, []),
    }, ["assignee_agent_id", "goal", "acceptance"])),
    "send_agent_message": ("向授权 Agent 发送任务消息", _object({
        "to_agent_id": _ID,
        "kind": {"type": "string", "enum": ["question", "progress", "result", "reply"]},
        "content": {"type": "string", "minLength": 1, "maxLength": 8000},
        "related_task_id": _ID,
        "reply_to_message_id": {"type": "integer", "minimum": 1},
    }, ["to_agent_id", "kind", "content"])),
    "get_task_status": ("读取可见任务状态", _object({"task_id": _ID}, ["task_id"])),
    "wait_for_tasks": ("等待自己派发的子任务", _object({
        "task_ids": {"type": "array", "items": _ID, "minItems": 1,
                     "maxItems": 4, "uniqueItems": True},
    }, ["task_ids"])),
    "cancel_task": ("取消自己或授权范围内的任务", _object({"task_id": _ID}, ["task_id"])),
    "request_human_approval": ("请求宿主用户作精确审批决定", _object({
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
    }, ["reason"])),
}
