"""P0 tool call 持久化顺序测试。"""

import copy
import sys
import unittest
from dataclasses import dataclass
from contextlib import contextmanager
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from backend import agent as agent_module
from backend.agent import Agent, AgentStep


class FakeCompletions:
    def __init__(self, chunks):
        self.chunks = chunks

    async def create(self, **_kwargs):
        async def stream():
            for chunk in self.chunks:
                yield chunk

        return stream()


def fake_client(calls, content=None, arguments="{}"):
    tool_calls = []
    for index, (call_id, name) in enumerate(calls):
        tool_calls.append(
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        )
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            )
        ]
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions([chunk]))
    )


@dataclass(frozen=True)
class FakeRunContext:
    call_id: str = ""


@contextmanager
def fake_gateway(calls, status="ok", model_text='{"ok": true}'):
    module = ModuleType("backend.tool_gateway")

    async def execute_tool(context, name, arguments):
        calls.append((context.call_id, name, arguments))
        return SimpleNamespace(
            call_id=context.call_id,
            status=status,
            effect="read_only",
            quiesced=status != "outcome_unknown",
            model_text=model_text,
            result_ref=None,
            error_code=None,
        )

    module.execute_tool = execute_tool
    with patch.dict(sys.modules, {"backend.tool_gateway": module}):
        yield


def make_agent(client, on_message, run_context=FakeRunContext()):
    with patch("backend.agent._get_shared_client", return_value=client):
        return Agent(
            api_key="test",
            base_url="https://example.test/v1",
            model="fake",
            max_rounds=1,
            on_message=on_message,
            log_stream=False,
            run_context=run_context,
        )


class AgentPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_call_ids_are_persisted_before_tools_and_stay_aligned(self):
        persisted = []

        async def persist(message):
            persisted.append(copy.deepcopy(message))

        agent = make_agent(
            fake_client(((None, "first_tool"), (None, "second_tool"))), persist
        )
        gateway_calls = []
        with fake_gateway(gateway_calls):
            events = [event async for event in agent.run([])]

        assistant = persisted[0]
        call_ids = [call["id"] for call in assistant["tool_calls"]]
        self.assertEqual(call_ids, ["call_1_1", "call_1_2"])
        self.assertEqual(len(set(call_ids)), 2)
        self.assertEqual(
            [message["tool_call_id"] for message in persisted[1:]], call_ids
        )
        self.assertEqual(
            gateway_calls,
            [("call_1_1", "first_tool", "{}"), ("call_1_2", "second_tool", "{}")],
        )
        self.assertEqual(
            [event["type"] for event in events].count("tool_result"), 2
        )

    async def test_assistant_persistence_failure_prevents_tool_execution(self):
        async def fail_persistence(_message):
            raise RuntimeError("database unavailable")

        agent = make_agent(fake_client(((None, "must_not_run"),)), fail_persistence)
        gateway_calls = []
        with fake_gateway(gateway_calls):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                async for _event in agent.run([]):
                    pass
        self.assertEqual(gateway_calls, [])

    async def test_duplicate_call_ids_fail_before_persistence_or_execution(self):
        persisted = []

        async def persist(message):
            persisted.append(message)

        agent = make_agent(
            fake_client((("duplicate", "first_tool"), ("duplicate", "second_tool"))),
            persist,
        )
        gateway_calls = []
        with fake_gateway(gateway_calls):
            with self.assertRaisesRegex(ValueError, "重复的 tool_call_id"):
                async for _event in agent.run([]):
                    pass
        self.assertEqual(persisted, [])
        self.assertEqual(gateway_calls, [])

    async def test_missing_context_fails_closed_before_gateway(self):
        persisted = []

        async def persist(message):
            persisted.append(copy.deepcopy(message))

        agent = make_agent(fake_client(((None, "denied_tool"),)), persist, None)
        gateway_calls = []
        with fake_gateway(gateway_calls):
            with self.assertRaisesRegex(RuntimeError, "缺少工具执行上下文"):
                async for _event in agent.run([]):
                    pass
        self.assertEqual(len(persisted), 1)
        self.assertEqual(gateway_calls, [])

    async def test_unknown_outcome_stops_before_next_tool(self):
        persisted = []

        async def persist(message):
            persisted.append(copy.deepcopy(message))

        agent = make_agent(
            fake_client(((None, "uncertain_tool"), (None, "must_not_run"))), persist
        )
        gateway_calls = []
        with fake_gateway(
            gateway_calls, status="outcome_unknown", model_text="outcome pending"
        ):
            events = [event async for event in agent.run([])]

        self.assertEqual(gateway_calls, [("call_1_1", "uncertain_tool", "{}")])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(events[-1]["type"], "error")

    async def test_agent_info_logs_do_not_repeat_model_or_tool_payloads(self):
        marker = "secret-marker-123"

        async def persist(_message):
            pass

        agent = make_agent(
            fake_client(
                ((None, "safe_tool"),),
                content=marker,
                arguments='{"payload":"' + marker + '"}',
            ),
            persist,
        )
        gateway_calls = []
        with fake_gateway(gateway_calls), self.assertLogs("agent.loop", "INFO") as logs:
            events = [event async for event in agent.run([])]

        self.assertEqual(len(gateway_calls), 1)
        self.assertTrue(events)
        self.assertNotIn(marker, "\n".join(logs.output))


class ProviderRedirectTest(unittest.TestCase):
    def test_model_client_does_not_follow_redirects_with_authorization(self):
        fake_http_client = object()
        fake_openai_client = object()
        with agent_module._client_lock:
            agent_module._clients.clear()
        with (
            patch.object(
                agent_module,
                "DefaultAsyncHttpxClient",
                return_value=fake_http_client,
            ) as http_client,
            patch.object(
                agent_module,
                "AsyncOpenAI",
                return_value=fake_openai_client,
            ) as openai_client,
        ):
            result = agent_module._get_shared_client(
                "provider-key", "https://api.example/v1"
            )
        with agent_module._client_lock:
            agent_module._clients.clear()

        http_client.assert_called_once_with(follow_redirects=False)
        openai_client.assert_called_once_with(
            api_key="provider-key",
            base_url="https://api.example/v1",
            http_client=fake_http_client,
        )
        self.assertIs(result, fake_openai_client)


class AgentStepTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_call_id_is_deterministic_and_tool_list_is_filtered(self):
        client = fake_client(((None, "get_current_time"),), content="hi")
        emitted = []
        async def on_delta(text):
            emitted.append(text)
        with patch("backend.agent._get_shared_client", return_value=client):
            step = AgentStep("key", "https://example.test", "model")
        result = await step([], run_id="run_1", round_index=2,
                            tool_names=("get_current_time",), on_delta=on_delta)
        self.assertEqual(result["tool_calls"][0]["id"], "call_run_1_2_1")
        self.assertEqual(result["content"], "hi")
        self.assertEqual(emitted, ["hi"])

    async def test_marker_like_call_id_is_rejected_before_return(self):
        client = fake_client((("secret marker", "get_current_time"),))
        with patch("backend.agent._get_shared_client", return_value=client):
            step = AgentStep("key", "https://example.test", "model")
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            await step([], run_id="run_1", round_index=1,
                       tool_names=("get_current_time",))


if __name__ == "__main__":
    unittest.main()
