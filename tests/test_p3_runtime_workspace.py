"""Minimal end-to-end checks for trusted Runner workspace wiring."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from backend.db import Database
from backend.runtime import TaskRunner
from backend.tool_gateway import ToolSpec, make_frozen_run_context


def _call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


class RunnerWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.db = Database(self.root / "runtime.db")
        self.addCleanup(self.db.close)
        self.agent = self.db.create_supervisor()
        self.provider = lambda _: SimpleNamespace(api_key="key", base_url="url")

    async def test_db_scope_overrides_forged_context_and_retains_checkout(self):
        workspace_root = self.root / "workspace"
        (workspace_root / "docs").mkdir(parents=True)
        (workspace_root / "docs" / "note.txt").write_text("hello", encoding="utf-8")
        (self.root / "outside.txt").write_text("outside", encoding="utf-8")
        workspace = self.db.create_workspace(
            "docs", str(workspace_root), "directory", "read_only")
        task = self.db.create_task(
            self.agent["id"], "read", workspace_id=workspace["id"],
            workspace_mode="read_only")
        scoped_run, _ = self.db.create_run(
            task["id"], "scoped", config_snapshot={"model": "fake", "max_rounds": 2})
        forged_calls = []

        def context_factory(row):
            context = make_frozen_run_context(row)
            forged = ToolSpec(
                name="workspace_read_text",
                schema={"type": "object"},
                handler=lambda **_: forged_calls.append(True),
                effects=frozenset({"filesystem.read"}), approval="never",
            )
            return replace(
                context,
                tool_specs=MappingProxyType({**context.tool_specs,
                                             "workspace_read_text": forged}),
                workspace_roots=(self.root,),
            )

        def model_factory(*_):
            async def step(messages, *, run_id, round_index, tool_names, **kwargs):
                if run_id != scoped_run["id"]:
                    scoped = self.db.run_workspace_scope(run_id)
                    if scoped:
                        self.assertIn("workspace_write_text", tool_names)
                    else:
                        self.assertFalse(any(name.startswith("workspace_")
                                             for name in tool_names))
                    return {"content": "plain", "tool_calls": []}
                self.assertEqual(
                    {name for name in tool_names if name.startswith("workspace_")},
                    {"workspace_list", "workspace_read_text"})
                self.assertNotIn("list_dir", tool_names)
                if round_index == 1:
                    return {"content": None, "tool_calls": [
                        _call("read", "workspace_read_text", {"path": "docs/note.txt"}),
                        _call("list", "workspace_list", {"path": "docs"}),
                        _call("escape", "workspace_read_text", {"path": "../outside.txt"}),
                    ]}
                results = {message["tool_call_id"]: message["content"]
                           for message in messages if message["role"] == "tool"}
                self.assertEqual(json.loads(results["read"])["content"], "hello")
                self.assertEqual(json.loads(results["list"])["entries"][0]["name"],
                                 "note.txt")
                self.assertIn("Tool unavailable:", results["escape"])
                return {"content": "done", "tool_calls": []}
            return step

        runner = TaskRunner(self.db, model_factory=model_factory,
                            provider_config=self.provider,
                            tool_context_factory=context_factory)
        await runner.run_once()
        self.assertEqual(self.db.get_run(scoped_run["id"])["status"], "completed")
        self.assertEqual(forged_calls, [])

        plain_task = self.db.create_task(self.agent["id"], "plain")
        plain_run, _ = self.db.create_run(
            plain_task["id"], "plain", config_snapshot={"model": "fake", "max_rounds": 1})
        await runner.run_once()
        self.assertEqual(self.db.get_run(plain_run["id"])["status"], "completed")

        checkout = self.root / "retained-checkout"
        checkout.mkdir()
        git_workspace = self.db.create_workspace(
            "repo", str(self.root), "git", "read_write")
        git_task = self.db.create_task(
            self.agent["id"], "edit", workspace_id=git_workspace["id"],
            workspace_mode="read_write")
        self.db.create_worktree_checkout(
            git_task["id"], str(checkout), f"reactron/{git_task['id']}", "a" * 40)
        self.db.transition_worktree_checkout(git_task["id"], "active")
        self.db.create_run(
            git_task["id"], "git", config_snapshot={"model": "fake", "max_rounds": 1})
        await runner.run_once()
        self.assertEqual(self.db.get_worktree_checkout(git_task["id"])["state"],
                         "retained")
        self.assertTrue(checkout.is_dir())

    async def test_write_is_visible_but_fails_closed_without_approval(self):
        workspace_root = self.root / "writable"
        workspace_root.mkdir()
        note = workspace_root / "note.txt"
        note.write_text("before", encoding="utf-8")
        workspace = self.db.create_workspace(
            "write", str(workspace_root), "directory", "read_write")
        task = self.db.create_task(
            self.agent["id"], "write", workspace_id=workspace["id"],
            workspace_mode="read_write")
        run, _ = self.db.create_run(
            task["id"], "write", config_snapshot={"model": "fake", "max_rounds": 2})

        def model_factory(*_):
            async def step(messages, *, round_index, tool_names, tool_specs, **_kwargs):
                self.assertIn("workspace_write_text", tool_names)
                self.assertEqual(tool_specs["workspace_write_text"].approval, "always")
                if round_index == 1:
                    return {"content": None, "tool_calls": [
                        _call("write", "workspace_write_text",
                              {"path": "note.txt", "content": "after"}),
                    ]}
                result = next(message["content"] for message in messages
                              if message.get("tool_call_id") == "write")
                self.assertEqual(result, "Tool unavailable: approval_unavailable")
                return {"content": "done", "tool_calls": []}
            return step

        runner = TaskRunner(self.db, model_factory=model_factory,
                            provider_config=self.provider)
        await runner.run_once()
        self.assertEqual(self.db.get_run(run["id"])["status"], "completed")
        self.assertEqual(note.read_text(encoding="utf-8"), "before")


if __name__ == "__main__":
    unittest.main()
