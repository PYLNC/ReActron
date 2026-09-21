"""Focused real-Git checks for the persisted worktree lifecycle."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db import Database, InvalidRunTransition, WorkspaceUnavailable
from backend.workspace import (
    cleanup_task_workspace,
    coordinate_git_worktrees,
    initialize_workspace_state,
    plan_git_worktree,
    WorkspaceError,
)


@unittest.skipUnless(shutil.which("git"), "git executable is required")
class GitLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repository = self.base / "repository"
        self.state = self.base / "state"
        self.repository.mkdir()
        self.state.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        (self.repository / "note.txt").write_text("committed\n", encoding="utf-8")
        self._git("add", "note.txt")
        self._git("commit", "-m", "initial")
        self.database = Database(self.base / "lifecycle.db")
        self.addCleanup(self.database.close)
        self.root = self.database.create_supervisor()
        self.workspace = self.database.create_workspace(
            "repo", str(self.repository), "git", "read_write"
        )

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments], check=True,
            shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def _task(self, goal):
        return self.database.create_task(
            self.root["id"], goal, workspace_id=self.workspace["id"],
            workspace_mode="read_write",
        )

    def test_two_tasks_are_isolated_and_terminal_checkout_is_only_retained(self):
        first, second = self._task("first"), self._task("second")
        (self.repository / "note.txt").write_text("main-dirty\n", encoding="utf-8")
        with patch("backend.workspace.STATE_ROOT", self.state):
            initialize_workspace_state()
            result = coordinate_git_worktrees(self.database)
            first_checkout = self.database.get_worktree_checkout(first["id"])
            second_checkout = self.database.get_worktree_checkout(second["id"])
            self.assertEqual(result["active"], 2)
            self.assertEqual((first_checkout["state"], second_checkout["state"]),
                             ("active", "active"))
            self.assertNotEqual(first_checkout["path"], second_checkout["path"])
            self.assertNotEqual(first_checkout["branch"], second_checkout["branch"])
            self.assertIsNone(self.database.get_latest_run_id(first["id"]))
            self.assertIsNone(self.database.get_latest_run_id(second["id"]))

            run, _ = self.database.create_run(first["id"], "first-run")
            running = self.database.claim_next_run("lifecycle-test", 120)
            self.assertEqual(running["id"], run["id"])
            self.database.transition_run(
                running["id"], running["version"], "completed", "run.completed", {},
                lease_owner="lifecycle-test", lease_epoch=running["lease_epoch"],
            )
            self.assertEqual(self.database.get_worktree_checkout(first["id"])["state"],
                             "retained")
            self.assertTrue(Path(first_checkout["path"]).is_dir())
            self.assertEqual(self.database.get_worktree_checkout(second["id"])["state"],
                             "active")
            cleanup_task_workspace(self.database, first["id"])

        self.assertFalse(Path(first_checkout["path"]).exists())
        self.assertTrue(Path(second_checkout["path"]).is_dir())
        self.assertEqual((self.repository / "note.txt").read_text(encoding="utf-8"),
                         "main-dirty\n")
        self.assertTrue(self._git("show-ref", "--verify",
                                  f"refs/heads/{first_checkout['branch']}"))

    def test_startup_finishes_creating_and_removing_and_records_failure(self):
        resumed = self._task("resume creating")
        with patch("backend.workspace.STATE_ROOT", self.state):
            initialize_workspace_state()
            plan = plan_git_worktree(self.repository, self.state, resumed["id"])
            self.database.create_worktree_checkout(
                resumed["id"], plan["checkout_path"], plan["branch"], plan["head"]
            )
            # Crash after Git created the branch but before it attached the checkout.
            self._git("branch", plan["branch"], plan["head"])
            result = coordinate_git_worktrees(self.database)
            self.assertEqual(result["active"], 1)
            resumed_row = self.database.get_worktree_checkout(resumed["id"])
            self.assertEqual(resumed_row["state"], "active")

            self.database.transition_worktree_checkout(resumed["id"], "removing")
            coordinate_git_worktrees(self.database)
            self.assertEqual(self.database.get_worktree_checkout(resumed["id"])["state"],
                             "removed")
            self.assertFalse(Path(resumed_row["path"]).exists())

            failed = self._task("record failure")
            failed_plan = plan_git_worktree(self.repository, self.state, failed["id"])
            self.database.create_worktree_checkout(
                failed["id"], failed_plan["checkout_path"], failed_plan["branch"],
                "f" * 40,
            )
            coordinate_git_worktrees(self.database)
            failed_row = self.database.get_worktree_checkout(failed["id"])
            self.assertEqual((failed_row["state"], failed_row["error_code"]),
                             ("failed", "git_failed"))
            self.assertFalse(Path(failed_plan["checkout_path"]).exists())

    def test_failed_and_cancelled_git_tasks_retry_in_their_retained_checkout(self):
        with patch("backend.workspace.STATE_ROOT", self.state):
            for terminal in ("failed", "cancelled"):
                with self.subTest(terminal=terminal):
                    task = self._task(terminal)
                    coordinate_git_worktrees(self.database)
                    checkout = self.database.get_worktree_checkout(task["id"])
                    checkout_path = Path(checkout["path"])
                    (checkout_path / "note.txt").write_text(
                        f"{terminal}-dirty\n", encoding="utf-8")
                    run, _ = self.database.create_run(task["id"], f"{terminal}-first")
                    running = self.database.claim_next_run("retry-test", 120)
                    self.assertEqual(running["id"], run["id"])
                    self.database.transition_run(
                        run["id"], running["version"], terminal, f"run.{terminal}",
                        {}, lease_owner="retry-test", lease_epoch=running["lease_epoch"])
                    self.assertEqual(self.database.get_worktree_checkout(task["id"])["state"],
                                     "retained")
                    with self.assertRaises(InvalidRunTransition):
                        self.database.create_run(task["id"], f"{terminal}-ordinary")
                    with patch("backend.workspace.inspect_git_worktree",
                               side_effect=WorkspaceError("worktree_mismatch")):
                        with self.assertRaises(WorkspaceUnavailable):
                            self.database.create_run(
                                task["id"], f"{terminal}-blocked", {"retry": True})
                    self.assertEqual(self.database.get_task(task["id"])["status"], terminal)
                    self.assertEqual(self.database.get_worktree_checkout(task["id"])["state"],
                                     "retained")
                    retried, created = self.database.create_run(
                        task["id"], f"{terminal}-retry", {"retry": True})
                    self.assertTrue(created)
                    self.assertEqual(retried["attempt"], 2)
                    self.assertEqual(self.database.get_task(task["id"])["status"], "open")
                    self.assertEqual(self.database.get_worktree_checkout(task["id"])["state"],
                                     "active")
                    self.assertEqual((checkout_path / "note.txt").read_text(encoding="utf-8"),
                                     f"{terminal}-dirty\n")
                    self.assertEqual((self.repository / "note.txt").read_text(encoding="utf-8"),
                                     "committed\n")
                    claimed = self.database.claim_next_run("retry-test", 120)
                    self.assertEqual(claimed["id"], retried["id"])
                    self.database.transition_run(
                        claimed["id"], claimed["version"], "completed", "run.completed", {},
                        lease_owner="retry-test", lease_epoch=claimed["lease_epoch"])


if __name__ == "__main__":
    unittest.main()
