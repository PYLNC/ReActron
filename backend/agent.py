"""最小 Agent 循环：流式调用模型，处理工具调用并继续下一轮。"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from openai import AsyncOpenAI

from backend.skills import TOOL_DEFINITIONS, execute_tool


MAX_ROUNDS = 5
DEFAULT_TOOL_TIMEOUT = 10.0
logger = logging.getLogger("agent.loop")

_clients = {}
_client_lock = threading.Lock()


def _get_shared_client(api_key: str, base_url: str) -> AsyncOpenAI:
    """按 API Key 和 Base URL 复用 AsyncOpenAI 客户端，避免每次请求重复建连。"""
    key = (api_key, base_url)
    with _client_lock:
        if key not in _clients:
            _clients[key] = AsyncOpenAI(api_key=api_key, base_url=base_url)
            logger.info("创建共享 AsyncOpenAI 客户端：base_url=%s", base_url)
        return _clients[key]


class Agent:
    """负责“模型回复 -> 工具执行 -> 结果回填”的循环。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        max_rounds: int = MAX_ROUNDS,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT,
        on_message: Optional[Callable] = None,
        log_stream: Optional[bool] = None,
        system_prompt: str = "",
    ):
        self.client = _get_shared_client(api_key, base_url)
        self.base_url = base_url
        self.model = model
        self.max_rounds = max(1, max_rounds)
        self.tool_timeout = max(0.1, tool_timeout)
        # 可选回调，用于把助手消息和工具消息写入数据库
        self.on_message = on_message
        self.system_prompt = system_prompt
        self.log_stream = os.getenv("LOG_STREAM", "1") == "1" if log_stream is None else log_stream
        logger.info(
            "Agent 已创建，model=%s，base_url=%s，max_rounds=%d，tool_timeout=%.1f，log_stream=%s",
            self.model,
            self.base_url,
            self.max_rounds,
            self.tool_timeout,
            self.log_stream,
        )

    def _log_messages(self, messages):
        """输出本轮发送给模型的完整消息输入。"""
        logger.info("模型输入消息数=%d", len(messages))
        for index, message in enumerate(messages):
            tool_calls = message.get("tool_calls")
            tool_calls_text = (
                json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else ""
            )
            logger.info(
                "模型输入[%d] role=%s content=%s tool_calls=%s tool_call_id=%s",
                index,
                message.get("role"),
                _truncate(message.get("content") or "", 300),
                _truncate(tool_calls_text, 300),
                message.get("tool_call_id"),
            )

    async def _notify_message(self, message: dict):
        """持久化单条消息；失败时终止当前 Agent step。"""
        if not self.on_message:
            return
        logger.info(
            "持久化消息：role=%s content=%s tool_calls=%s tool_call_id=%s",
            message.get("role"),
            _truncate(message.get("content") or "", 300),
            _truncate(
                json.dumps(message.get("tool_calls"), ensure_ascii=False, default=str)
                if message.get("tool_calls")
                else "",
                300,
            ),
            message.get("tool_call_id"),
        )
        await self.on_message(message)

    async def _execute_tool(self, call: dict, round_index: int, call_index: int) -> dict:
        """并发执行单个工具调用，返回统一的执行结果。"""
        name = call["function"]["name"]
        raw_arguments = call["function"]["arguments"]
        logger.info(
            "工具输入：name=%s，arguments=%s",
            name,
            _truncate(raw_arguments, 300),
        )
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            result = await asyncio.wait_for(
                asyncio.to_thread(execute_tool, name, arguments),
                timeout=self.tool_timeout,
            )
            result_text = json.dumps(result, ensure_ascii=False)
            logger.info(
                "工具输出：name=%s，ok=True，result=%s",
                name,
                _truncate(result_text, 500),
            )
            return {"ok": True, "name": name, "message": result_text}
        except asyncio.TimeoutError:
            result_text = f"调用失败: 工具执行超时（{self.tool_timeout}s）"
            logger.warning("工具执行超时，name=%s", name)
            return {"ok": False, "name": name, "message": result_text}
        except Exception as exc:
            result_text = f"调用失败: {exc}"
            logger.warning("工具执行失败，name=%s，error=%s", name, exc)
            return {"ok": False, "name": name, "message": result_text}

    async def run(self, messages):
        """逐轮与模型对话，产出一系列供前端展示的 SSE 事件。"""
        current_messages = list(messages)
        if self.system_prompt and not any(
            message.get("role") == "system" for message in current_messages
        ):
            current_messages.insert(0, {
                "role": "system",
                "content": self.system_prompt,
            })
        agent_started = time.perf_counter()
        logger.info(
            "Agent 开始运行，初始消息数=%d，最大轮数=%d",
            len(current_messages),
            self.max_rounds,
        )

        for round_index in range(1, self.max_rounds + 1):
            round_started = time.perf_counter()
            logger.info("开始第 %d/%d 轮", round_index, self.max_rounds)
            yield {"type": "round", "round": round_index}

            logger.info(
                "发送模型请求：round=%d，messages=%d，tools=%d",
                round_index,
                len(current_messages),
                len(TOOL_DEFINITIONS),
            )
            self._log_messages(current_messages)

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=current_messages,
                tools=TOOL_DEFINITIONS,
                stream=True,
            )
            logger.info("模型流式响应已建立，round=%d", round_index)

            content_parts = []
            tool_calls = {}
            chunk_count = 0
            finish_reason = None

            # 流式读取时，文本按片段输出；工具调用参数也会分片拼接
            async for chunk in response:
                chunk_count += 1
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta

                if self.log_stream:
                    delta_tool_calls = []
                    if delta and delta.tool_calls:
                        for item in delta.tool_calls:
                            function = item.function
                            delta_tool_calls.append({
                                "index": item.index,
                                "id": item.id,
                                "name": function.name if function else None,
                                "arguments": function.arguments if function else None,
                            })
                    logger.info(
                        "[stream] round=%d chunk=%d finish_reason=%s text=%s tool_calls=%s",
                        round_index,
                        chunk_count,
                        finish_reason,
                        _truncate(delta.content if delta and delta.content else "", 200),
                        _truncate(
                            json.dumps(delta_tool_calls, ensure_ascii=False, default=str),
                            300,
                        ),
                    )

                if delta and delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "text", "content": delta.content}

                if delta and delta.tool_calls:
                    for item in delta.tool_calls:
                        index = item.index
                        # 同一个工具调用可能分成多个 chunk，所以按 index 累积
                        entry = tool_calls.setdefault(index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if item.id:
                            entry["id"] = item.id
                        if item.function:
                            if item.function.name:
                                entry["function"]["name"] += item.function.name
                            if item.function.arguments:
                                entry["function"]["arguments"] += item.function.arguments

            ordered_calls = [tool_calls[i] for i in sorted(tool_calls)]
            call_ids = set()
            for call_index, call in enumerate(ordered_calls, start=1):
                call["id"] = call.get("id") or f"call_{round_index}_{call_index}"
                if call["id"] in call_ids:
                    raise ValueError(f"模型返回重复的 tool_call_id：{call['id']}")
                call_ids.add(call["id"])
            elapsed = time.perf_counter() - round_started
            logger.info(
                "模型输出摘要：round=%d，chunks=%d，文本=%s，工具调用=%s，"
                "finish_reason=%s，耗时=%.2fs",
                round_index,
                chunk_count,
                _truncate("".join(content_parts), 500),
                _truncate(
                    json.dumps(ordered_calls, ensure_ascii=False, default=str),
                    500,
                ),
                finish_reason,
                elapsed,
            )

            assistant_message = {
                "role": "assistant",
                "content": "".join(content_parts) or None,
            }

            if not ordered_calls:
                # 这一轮没有工具调用，说明模型已经给出最终回答
                await self._notify_message(assistant_message)
                logger.info("第 %d 轮没有工具调用，Agent 结束", round_index)
                total_ms = int((time.perf_counter() - agent_started) * 1000)
                logger.info("Agent 结束：rounds=%d，total_ms=%d", round_index, total_ms)
                yield {"type": "done", "rounds": round_index, "total_ms": total_ms}
                return

            # 模型本轮调用了工具，需要把 assistant 消息完整保存进历史
            assistant_message["tool_calls"] = ordered_calls
            current_messages.append(assistant_message)
            await self._notify_message(assistant_message)

            tasks = [
                asyncio.create_task(self._execute_tool(call, round_index, call_index))
                for call_index, call in enumerate(ordered_calls, start=1)
            ]

            for call_index, (call, task) in enumerate(zip(ordered_calls, tasks), start=1):
                name = call["function"]["name"]
                raw_arguments = call["function"]["arguments"]
                yield {"type": "tool_call", "name": name, "arguments": raw_arguments}

                result = await task
                ok = result["ok"]
                result_text = result["message"]
                yield {
                    "type": "tool_result",
                    "name": name,
                    "ok": ok,
                    "message": _truncate(result_text),
                }

                tool_message = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result_text,
                }
                current_messages.append(tool_message)
                await self._notify_message(tool_message)
                logger.info(
                    "工具结果回填：name=%s，content=%s，当前消息数=%d",
                    name,
                    _truncate(result_text, 300),
                    len(current_messages),
                )

        logger.warning("已达到最大轮数 %d，Agent 提前结束", self.max_rounds)
        total_ms = int((time.perf_counter() - agent_started) * 1000)
        yield {
            "type": "error",
            "message": f"已达到最大轮数 {self.max_rounds}",
            "total_ms": total_ms,
        }


def _truncate(text: str, limit: int = 500):
    """限制工具结果长度，避免把前端和上下文撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"
