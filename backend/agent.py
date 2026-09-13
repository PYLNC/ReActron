"""最小 Agent 循环：流式调用模型，处理工具调用并继续下一轮。"""

import logging
import os
import re
import threading
import time
from dataclasses import replace
from typing import Callable, Optional

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from backend.skills import TOOL_DEFINITIONS


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
            _clients[key] = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=DefaultAsyncHttpxClient(follow_redirects=False),
            )
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
        run_context=None,
    ):
        self.client = _get_shared_client(api_key, base_url)
        self.base_url = base_url
        self.model = model
        self.max_rounds = max(1, max_rounds)
        self.tool_timeout = max(0.1, tool_timeout)
        # 可选回调，用于把助手消息和工具消息写入数据库
        self.on_message = on_message
        self.system_prompt = system_prompt
        self.run_context = run_context
        self.log_stream = os.getenv("LOG_STREAM", "1") == "1" if log_stream is None else log_stream
        logger.info(
            "Agent 已创建，model=%s，base_url=%s，max_rounds=%d，tool_timeout=%.1f，log_stream=%s",
            self.model,
            self.base_url,
            self.max_rounds,
            self.tool_timeout,
            self.log_stream,
        )

    async def _notify_message(self, message: dict):
        """持久化单条消息；失败时终止当前 Agent step。"""
        if not self.on_message:
            return
        await self.on_message(message)

    async def _execute_tool(self, call: dict):
        """唯一工具入口；Gateway 负责参数、权限、超时与结果脱敏。"""
        if self.run_context is None:
            raise RuntimeError("缺少工具执行上下文")
        from backend.tool_gateway import execute_tool

        return await execute_tool(
            replace(self.run_context, call_id=call["id"]),
            call["function"]["name"],
            call["function"]["arguments"],
        )

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
                    logger.info(
                        "[stream] round=%d chunk=%d finish_reason=%s text_chars=%d tool_calls=%d",
                        round_index,
                        chunk_count,
                        finish_reason,
                        len(delta.content or "") if delta else 0,
                        len(delta.tool_calls or ()) if delta else 0,
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
                "模型输出摘要：round=%d，chunks=%d，文本字符数=%d，工具调用数=%d，"
                "finish_reason=%s，耗时=%.2fs",
                round_index,
                chunk_count,
                sum(map(len, content_parts)),
                len(ordered_calls),
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

            for call in ordered_calls:
                name = call["function"]["name"]
                raw_arguments = call["function"]["arguments"]
                yield {"type": "tool_call", "name": name, "arguments": raw_arguments}

                outcome = await self._execute_tool(call)
                if outcome.call_id != call["id"]:
                    raise ValueError("工具结果 call_id 不匹配")
                ok = outcome.status == "ok"
                result_text = outcome.model_text
                yield {
                    "type": "tool_result",
                    "name": name,
                    "ok": ok,
                    "message": result_text,
                }

                if outcome.status == "outcome_unknown":
                    yield {"type": "error", "message": "工具结果未确认，需要人工核实"}
                    return

                tool_message = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result_text,
                }
                current_messages.append(tool_message)
                await self._notify_message(tool_message)
                logger.info(
                    "工具结果回填：name=%s，content_chars=%d，当前消息数=%d",
                    name,
                    len(result_text),
                    len(current_messages),
                )

        logger.warning("已达到最大轮数 %d，Agent 提前结束", self.max_rounds)
        total_ms = int((time.perf_counter() - agent_started) * 1000)
        yield {
            "type": "error",
            "message": f"已达到最大轮数 {self.max_rounds}",
            "total_ms": total_ms,
        }


class AgentStep:
    """One model invocation; the Runner owns all persistence and tool execution."""

    def __init__(self, api_key, base_url, model):
        self.client = _get_shared_client(api_key, base_url)
        self.model = model

    async def __call__(self, messages, *, run_id, round_index, tool_names=(), on_delta=None):
        definitions = [item for item in TOOL_DEFINITIONS
                       if item["function"]["name"] in tool_names]
        kwargs = {"model": self.model, "messages": messages, "stream": True}
        if definitions:
            kwargs["tools"] = definitions
        stream = await self.client.chat.completions.create(**kwargs)
        content_parts, tool_calls = [], {}
        total_chars = 0
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta:
                continue
            if delta.content:
                total_chars += len(delta.content)
                if total_chars > 64_000:
                    raise ValueError("模型单步输出超过 64k 字符")
                content_parts.append(delta.content)
                if on_delta is not None:
                    await on_delta(delta.content)
            for item in delta.tool_calls or ():
                if not isinstance(item.index, int) or item.index < 0 or item.index >= 32:
                    raise ValueError("无效工具调用序号")
                call = tool_calls.setdefault(item.index, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if item.id:
                    call["id"] = item.id
                if item.function:
                    for field in ("name", "arguments"):
                        fragment = getattr(item.function, field, None)
                        if fragment:
                            total_chars += len(fragment)
                            if total_chars > 64_000:
                                raise ValueError("模型单步输出超过 64k 字符")
                            call["function"][field] += fragment
        ordered = [tool_calls[index] for index in sorted(tool_calls)]
        ids = set()
        for index, call in enumerate(ordered, 1):
            call["id"] = call["id"] or f"call_{run_id}_{round_index}_{index}"
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call["id"]) or
                    call["id"] in ids or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,80}", call["function"]["name"])):
                raise ValueError("重复或无效的 tool_call_id/name")
            ids.add(call["id"])
        return {"content": "".join(content_parts) or None, "tool_calls": ordered}
