"""文件 Skill：读取上传文件解析结果。"""

import json
import logging

from backend.db import db


logger = logging.getLogger("agent.skills.file")


def read_uploaded_file(file_id: str):
    """读取已上传文件的解析结果。"""
    row = db.get_upload(file_id)
    if not row:
        raise ValueError(f"上传文件不存在: {file_id}")
    parsed = json.loads(row["parsed_json"]) if row["parsed_json"] else {}
    logger.info("读取上传文件：file_id=%s，filename=%s", file_id, row["filename"])
    return {
        "file_id": row["id"],
        "filename": row["filename"],
        "size": row["size"],
        "parsed": parsed,
    }


FILE_SKILL = {
    "name": "file",
    "description": "读取上传文件的解析结果。",
    "instructions": "用户上传文件后，通过 file_id 调用 read_uploaded_file 获取解析内容。",
    "tool_definitions": [
        {
            "type": "function",
            "function": {
                "name": "read_uploaded_file",
                "description": "读取已上传文件的解析结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "上传接口返回的 file_id"},
                    },
                    "required": ["file_id"],
                },
            },
        },
    ],
    "tool_functions": {
        "read_uploaded_file": read_uploaded_file,
    },
}
