"""Harness 基础单元测试。"""

import tempfile
import unittest
from pathlib import Path

from backend.harness import Harness, HarnessError, PathPolicy


class PathPolicyTest(unittest.TestCase):
    def test_allowed_file_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = PathPolicy(allowed_roots=(root,), allowed_extensions=(".xlsx",))
            resolved = policy.resolve(str(root / "ok.xlsx"), operation="excel_create_file")
            self.assertEqual(resolved, (root / "ok.xlsx").resolve())

    def test_reject_file_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = Path(tempfile.gettempdir()) / "harness_outside_test.xlsx"
            policy = PathPolicy(allowed_roots=(root,), allowed_extensions=(".xlsx",))
            with self.assertRaises(HarnessError):
                policy.resolve(str(outside), operation="excel_create_file")

    def test_reject_deny_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = PathPolicy(allowed_roots=(root,), allowed_extensions=(".xlsx",))
            with self.assertRaises(HarnessError):
                policy.resolve(str(root / ".secret_key"), operation="excel_create_file")

    def test_monitor_allows_and_enforce_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = Path(tempfile.gettempdir()) / "harness_outside_test.xlsx"
            policy = PathPolicy(allowed_roots=(root,), allowed_extensions=(".xlsx",))

            monitor = Harness(mode="monitor", policy=policy, record=False)
            monitor.check_tool_call("excel_create_file", {"file_path": str(outside)})

            enforce = Harness(mode="enforce", policy=policy, record=False)
            with self.assertRaises(HarnessError):
                enforce.check_tool_call("excel_create_file", {"file_path": str(outside)})


if __name__ == "__main__":
    unittest.main()
