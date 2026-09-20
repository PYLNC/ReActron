"""Security checks for trusted Workspace scopes and Git worktree primitives."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.tool_gateway import RunContext, execute_tool
from backend.workspace import (
    MAX_TEXT_BYTES,
    WorkspaceError,
    _is_reparse,
    cleanup_task_workspace,
    create_git_worktree,
    get_task_diff,
    git_worktree_diff,
    initialize_workspace_state,
    inspect_git_worktree,
    list_workspace_directory,
    normalize_workspace_root,
    read_workspace_text,
    remove_git_worktree,
    resolve_workspace_path,
    workspace_tool_specs,
    write_workspace_text,
)


class WorkspacePathTest(unittest.TestCase):
    def test_state_initialization_uses_only_fixed_plain_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve() / "state"
            with patch("backend.workspace.STATE_ROOT", state):
                self.assertIsNone(initialize_workspace_state())
                self.assertIsNone(initialize_workspace_state())
            self.assertTrue((state / "worktrees").is_dir())
            self.assertTrue((state / "artifacts").is_dir())

            (state / "artifacts").rmdir()
            (state / "artifacts").write_text("not a directory", encoding="utf-8")
            with patch("backend.workspace.STATE_ROOT", state), \
                    self.assertRaises(WorkspaceError) as caught:
                initialize_workspace_state()
            self.assertEqual(caught.exception.code, "workspace_scope_invalid")

            target, alias = Path(temporary) / "target", Path(temporary) / "alias"
            target.mkdir()
            try:
                alias.symlink_to(target, target_is_directory=True)
            except OSError:
                return
            with patch("backend.workspace.STATE_ROOT", alias / "state"), \
                    self.assertRaises(WorkspaceError):
                initialize_workspace_state()
            self.assertFalse((target / "state").exists())

    def test_strict_relative_paths_and_windows_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "folder").mkdir()
            expected = root / "folder" / "note.txt"
            expected.write_text("ok", encoding="utf-8")
            self.assertEqual(resolve_workspace_path(root, "folder/note.txt"), expected)
            self.assertEqual(resolve_workspace_path(root, "folder\\note.txt"), expected)
            rejected = (
                "", ".", "..", "../outside", "folder/../outside", "folder//note.txt",
                "/outside", "\\outside", "C:\\outside", "C:outside",
                "\\\\server\\share\\file", "folder/note.txt:stream", "folder/NUL.txt",
                "folder/trailing.\x00", "folder/trailing.", "folder/trailing ",
            )
            for raw in rejected:
                with self.subTest(raw=raw), self.assertRaises(WorkspaceError):
                    resolve_workspace_path(root, raw, for_write=True)

    def test_symlink_and_windows_reparse_are_rejected(self):
        fake = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(_is_reparse(fake))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(WorkspaceError):
                normalize_workspace_root(link, "directory")
            with self.assertRaises(WorkspaceError) as caught:
                resolve_workspace_path(root, "link/file.txt", for_write=True)
            self.assertEqual(caught.exception.code, "path_not_granted")

    def test_capped_utf8_io_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "docs").mkdir()
            path = resolve_workspace_path(root, "docs/note.txt", for_write=True)
            self.assertEqual(write_workspace_text(root, path, "你好")["bytes"], 6)
            self.assertEqual(read_workspace_text(root, path)["content"], "你好")
            with self.assertRaises(WorkspaceError) as caught:
                write_workspace_text(root, path, "\ud800")
            self.assertEqual(caught.exception.code, "invalid_utf8")
            path.write_bytes(b"\xff")
            with self.assertRaises(WorkspaceError) as caught:
                read_workspace_text(root, path)
            self.assertEqual(caught.exception.code, "invalid_utf8")
            with self.assertRaises(WorkspaceError) as caught:
                write_workspace_text(root, path, "x" * (MAX_TEXT_BYTES + 1))
            self.assertEqual(caught.exception.code, "limit_exceeded")
            path.write_text("original", encoding="utf-8")
            with patch("backend.workspace.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(WorkspaceError):
                    write_workspace_text(root, path, "replacement")
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(list((root / "docs").glob(".reactron-*")), [])

    def test_directory_listing_is_bounded_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / "docs"
            directory.mkdir()
            (directory / "a.txt").write_text("a", encoding="utf-8")
            (directory / "nested").mkdir()
            result = list_workspace_directory(root, directory)
            self.assertEqual(result["path"], "docs")
            self.assertEqual({item["name"]: item["type"] for item in result["entries"]},
                             {"a.txt": "file", "nested": "directory"})
            (directory / "b.txt").write_text("b", encoding="utf-8")
            with patch("backend.workspace.MAX_LIST_ENTRIES", 1):
                with self.assertRaises(WorkspaceError) as caught:
                    list_workspace_directory(root, directory)
            self.assertEqual(caught.exception.code, "limit_exceeded")


class WorkspaceToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_scope_controls_visibility_grants_and_canonical_handler_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "docs").mkdir()
            file_path = root / "docs" / "note.txt"
            file_path.write_text("hello", encoding="utf-8")
            scope = {"workspace_id": "workspace-1", "kind": "directory",
                     "effective_root": root, "mode": "read_only"}
            self.assertEqual(workspace_tool_specs(None), {})
            self.assertEqual(workspace_tool_specs({**scope, "effective_root": "relative"}), {})
            specs = workspace_tool_specs(scope)
            self.assertEqual(set(specs), {"workspace_list", "workspace_read_text"})
            self.assertEqual(specs["workspace_read_text"].effects,
                             frozenset({"filesystem.read"}))
            context = RunContext("run", tool_specs=specs,
                                 grants=frozenset({"filesystem.read"}))
            outcome = await execute_tool(context, "workspace_read_text",
                                         {"path": "docs/note.txt"})
            self.assertEqual(outcome.status, "ok")
            self.assertIn("hello", outcome.model_text)
            listed = await execute_tool(context, "workspace_list", {"path": "docs"})
            self.assertEqual(listed.status, "ok")
            self.assertIn("note.txt", listed.model_text)
            outside = await execute_tool(context, "workspace_read_text", {"path": "../note.txt"})
            self.assertEqual(outside.error_code, "tool_failed")

            with patch("backend.workspace.read_workspace_text",
                       return_value={"content": "ok"}) as reader:
                specs["workspace_read_text"].handler(path="docs/note.txt")
            self.assertEqual(reader.call_args.args, (root, file_path))

            writable = workspace_tool_specs({**scope, "mode": "read_write"})
            self.assertEqual(writable["workspace_write_text"].effects,
                             frozenset({"filesystem.write"}))
            self.assertEqual(writable["workspace_write_text"].approval, "always")
            write_context = RunContext("run", tool_specs=writable,
                                       grants=frozenset({"filesystem.write"}))
            rejected = await execute_tool(write_context, "workspace_write_text",
                                          {"path": "docs/note.txt", "content": "changed"})
            self.assertEqual(rejected.error_code, "approval_unavailable")
            self.assertEqual(file_path.read_text(encoding="utf-8"), "hello")


@unittest.skipUnless(shutil.which("git"), "git executable is required")
class GitWorktreeTest(unittest.TestCase):
    def _git(self, root, *arguments):
        return subprocess.run(
            ["git", "-C", str(root), *arguments], check=True, shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def test_isolated_lifecycle_preserves_main_checkout_and_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repository, state = base / "repository", base / "state"
            repository.mkdir()
            state.mkdir()
            with patch("backend.workspace.STATE_ROOT", state):
                initialize_workspace_state()
            self._git(repository, "init")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "config", "user.name", "Test")
            tracked = repository / "note.txt"
            tracked.write_text("committed\n", encoding="utf-8")
            self._git(repository, "add", "note.txt")
            self._git(repository, "commit", "-m", "initial")
            head = self._git(repository, "rev-parse", "--verify", "HEAD")
            tracked.write_text("main-uncommitted\n", encoding="utf-8")

            real_popen = subprocess.Popen
            with patch.dict(os.environ, {"GIT_DIR": str(base / "outside")}), \
                    patch("backend.workspace.subprocess.Popen", wraps=real_popen) as popen:
                created = create_git_worktree(repository, state, "task_1")
                checkout = Path(created["checkout_path"])
                self.assertEqual(checkout, state / "worktrees" / "task_1")
                self.assertEqual(created["branch"], "reactron/task_1")
                self.assertEqual(created["head"], head)
                self.assertFalse(inspect_git_worktree(repository, state, "task_1")["dirty"])
                (checkout / "note.txt").write_text("worker-change\n", encoding="utf-8")
                self.assertIn("worker-change", git_worktree_diff(repository, state, "task_1"))
                with self.assertRaises(WorkspaceError) as caught:
                    git_worktree_diff(repository, state, "task_1", max_bytes=8)
                self.assertEqual(caught.exception.code, "limit_exceeded")
                with self.assertRaises(WorkspaceError) as caught:
                    remove_git_worktree(repository, state, "task_1", has_active_runs=False)
                self.assertEqual(caught.exception.code, "worktree_dirty")
                (checkout / "note.txt").write_text("committed\n", encoding="utf-8")
                with self.assertRaises(WorkspaceError) as caught:
                    remove_git_worktree(repository, state, "task_1", has_active_runs=True)
                self.assertEqual(caught.exception.code, "workspace_in_use")
                removed = remove_git_worktree(repository, state, "task_1", has_active_runs=False)

            self.assertTrue(removed["removed"])
            self.assertFalse(checkout.exists())
            self.assertEqual(tracked.read_text(encoding="utf-8"), "main-uncommitted\n")
            self.assertTrue(self._git(repository, "show-ref", "--verify",
                                      "refs/heads/reactron/task_1"))
            commands = [call.args[0] for call in popen.call_args_list]
            self.assertTrue(all(isinstance(command, list) for command in commands))
            self.assertTrue(all(call.kwargs.get("shell") is False
                                for call in popen.call_args_list))
            self.assertTrue(all("GIT_DIR" not in call.kwargs["env"]
                                for call in popen.call_args_list))
            self.assertNotIn("--force", [part for command in commands for part in command])

    def test_invalid_task_id_never_invokes_git_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, state = root / "repository", root / "state"
            repository.mkdir()
            state.mkdir()
            self._git(repository, "init")
            with self.assertRaises(WorkspaceError) as caught:
                create_git_worktree(repository, state, "../escape")
            self.assertEqual(caught.exception.code, "invalid_task_id")
            with self.assertRaises(WorkspaceError) as caught:
                create_git_worktree(repository, state, "task_1")
            self.assertEqual(caught.exception.code, "workspace_scope_invalid")
            self.assertFalse((state / "worktrees").exists())

    def test_registration_and_api_services_only_use_stored_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repository, state = base / "repository", base / "state"
            repository.mkdir()
            state.mkdir()
            with patch("backend.workspace.STATE_ROOT", state):
                initialize_workspace_state()
            self._git(repository, "init")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "config", "user.name", "Test")
            (repository / "note.txt").write_text("one\n", encoding="utf-8")
            self._git(repository, "add", "note.txt")
            self._git(repository, "commit", "-m", "initial")
            self.assertEqual(normalize_workspace_root(repository, "git"), repository)
            (repository / "nested").mkdir()
            with self.assertRaises(WorkspaceError):
                normalize_workspace_root(repository / "nested", "git")

            created = create_git_worktree(repository, state, "task_2")
            checkout = Path(created["checkout_path"])
            (checkout / "note.txt").write_text("two\n", encoding="utf-8")

            class Database:
                def __init__(self, checkout_path):
                    self.active = True
                    self.race = False
                    self.checkout = {
                        "task_id": "task_2", "workspace_id": "workspace-1",
                        "path": str(checkout_path), "branch": "reactron/task_2",
                        "state": "retained",
                    }
                    self.transitions = []

                def get_task(self, task_id):
                    return {"id": task_id, "workspace_id": "workspace-1",
                            "status": "completed"}

                def get_workspace(self, workspace_id):
                    return {"id": workspace_id, "kind": "git",
                            "root_path": str(repository)}

                def get_worktree_checkout(self, _task_id):
                    return dict(self.checkout)

                def task_has_active_run(self, _task_id):
                    return False if self.race else self.active

                def transition_worktree_checkout(self, _task_id, state_name, error_code=None):
                    if state_name == "removing" and (self.active or self.race):
                        raise RuntimeError("storage atomically rejected active run")
                    self.checkout["state"] = state_name
                    self.transitions.append((state_name, error_code))
                    return dict(self.checkout)

            database = Database(checkout)
            with patch("backend.workspace.STATE_ROOT", state):
                self.assertIn("two", get_task_diff(database, "task_2"))
                database.checkout["path"] = str(repository)
                with self.assertRaises(WorkspaceError) as caught:
                    get_task_diff(database, "task_2")
                self.assertEqual(caught.exception.code, "worktree_mismatch")
                database.checkout["path"] = str(checkout)
                with self.assertRaises(WorkspaceError) as caught:
                    cleanup_task_workspace(database, "task_2")
                self.assertEqual(caught.exception.code, "workspace_in_use")
                self.assertEqual(database.transitions, [])
                database.race = True
                with self.assertRaises(RuntimeError):
                    cleanup_task_workspace(database, "task_2")
                self.assertTrue(checkout.exists())
                self.assertEqual(database.transitions, [])
                database.race = False
                database.active = False
                with self.assertRaises(WorkspaceError) as caught:
                    cleanup_task_workspace(database, "task_2")
                self.assertEqual(caught.exception.code, "worktree_dirty")
                self.assertTrue(checkout.exists())
                (checkout / "note.txt").write_text("one\n", encoding="utf-8")
                self.assertTrue(cleanup_task_workspace(database, "task_2")["removed"])
            self.assertEqual(database.transitions, [
                ("removing", None), ("failed", "worktree_dirty"),
                ("removing", None), ("removed", None),
            ])
            self.assertFalse(checkout.exists())
            self.assertTrue(self._git(repository, "show-ref", "--verify",
                                      "refs/heads/reactron/task_2"))


if __name__ == "__main__":
    unittest.main()
