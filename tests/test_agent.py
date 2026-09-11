"""P0 tool call 持久化顺序测试。"""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent import Agent


class FakeCompletions:
    def __init__(self, chunks):
        self.chunks = chunks

    async def create(self, **_kwargs):
        async def stream():
            for chunk in self.chunks:
                yield chunk

        return stream()


def fake_client(calls):
    tool_calls = []
    for index, (call_id, name) in enumerate(calls):
        tool_calls.append(
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments="{}"),
            )
        )
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                delta=SimpleNamespace(content=None, tool_calls=tool_calls),
            )
        ]
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions([chunk]))
    )


def make_agent(client, on_message):
    with patch("backend.agent._get_shared_client", return_value=client):
        return Agent(
            api_key="test",
            base_url="https://example.test/v1",
            model="fake",
            max_rounds=1,
            on_message=on_message,
            log_stream=False,
        )


class AgentPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_call_ids_are_persisted_before_tools_and_stay_aligned(self):
        persisted = []

        async def persist(message):
            persisted.append(copy.deepcopy(message))

        agent = make_agent(
            fake_client(((None, "first_tool"), (None, "second_tool"))), persist
        )
        with patch("backend.agent.execute_tool", return_value={"ok": True}) as execute:
            events = [event async for event in agent.run([])]

        assistant = persisted[0]
        call_ids = [call["id"] for call in assistant["tool_calls"]]
        self.assertEqual(call_ids, ["call_1_1", "call_1_2"])
        self.assertEqual(len(set(call_ids)), 2)
        self.assertEqual(
            [message["tool_call_id"] for message in persisted[1:]], call_ids
        )
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            [event["type"] for event in events].count("tool_result"), 2
        )

    async def test_assistant_persistence_failure_prevents_tool_execution(self):
        async def fail_persistence(_message):
            raise RuntimeError("database unavailable")

        agent = make_agent(fake_client(((None, "must_not_run"),)), fail_persistence)
        with patch("backend.agent.execute_tool") as execute:
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                async for _event in agent.run([]):
                    pass
        execute.assert_not_called()

    async def test_duplicate_call_ids_fail_before_persistence_or_execution(self):
        persisted = []

        async def persist(message):
            persisted.append(message)

        agent = make_agent(
            fake_client((("duplicate", "first_tool"), ("duplicate", "second_tool"))),
            persist,
        )
        with patch("backend.agent.execute_tool") as execute:
            with self.assertRaisesRegex(ValueError, "重复的 tool_call_id"):
                async for _event in agent.run([]):
                    pass
        self.assertEqual(persisted, [])
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
