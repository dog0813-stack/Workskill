"""Offline tests; no mailbox, OA, account or network is accessed."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "task_state.py"
REPO = SCRIPT.parents[3]
SPEC = importlib.util.spec_from_file_location("expense_task_state", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures outside Git. Set EXPENSE_TEST_ROOT to choose another parent.
        parent = Path(os.environ.get("EXPENSE_TEST_ROOT", str(REPO.parent)))
        self.temporary = tempfile.TemporaryDirectory(prefix="expense-state-tests-", dir=parent)
        self.base = Path(self.temporary.name)
        self.root = self.base / "private"
        self.employee = "employee-a"
        self.task = "trip-a"

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, employee=None, task=None, root=None):
        return [
            sys.executable, str(SCRIPT), "--root", str(root or self.root),
            "--employee", employee or self.employee, "--task", task or self.task, *args,
        ]

    def call(self, *args, ok=True, **scope):
        result = subprocess.run(self.command(*args, **scope), capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0 if ok else 2, (result.stdout or "") + (result.stderr or ""))
        return json.loads(result.stdout)["state"] if ok else result

    @property
    def state_path(self):
        return self.root / self.employee / (self.task + ".json")

    def draft(self, intent="submit"):
        self.call("init", "--intent", intent)
        self.call("checkpoint", "--stage", "materials", "--evidence", "record:materials-checked")
        return self.call("checkpoint", "--stage", "draft", "--evidence", "record:draft-reopened")

    def begin(self, ok=True, **overrides):
        values = {
            "document-id": "draft-123", "amount": "21.50", "currency": "CNY",
            "authorization": "conversation:approved-draft-123-21.50",
            "evidence": "record:amount-identity-attachments-checked",
        }
        values.update(overrides)
        args = ["begin-submit"]
        for key, value in values.items():
            args.extend(["--" + key, value])
        return self.call(*args, ok=ok)

    def test_init_resumes_without_reset_and_rejects_implicit_intent_change(self):
        self.draft()
        before = self.state_path.read_bytes()
        self.call("init", "--intent", "submit")
        self.assertEqual(before, self.state_path.read_bytes())
        self.call("init", "--intent", "draft", ok=False)
        self.assertEqual(before, self.state_path.read_bytes())

    def test_explicit_intent_change_preserves_draft_then_allows_submit(self):
        self.draft(intent="draft")
        self.begin(ok=False)
        state = self.call("set-intent", "--intent", "submit", "--authorization", "conversation:submit-request")
        self.assertEqual(state["stage"], "draft")
        self.assertEqual(state["events"][-1]["action"], "set-intent")
        self.assertEqual(self.begin()["status"], "submitting")

    def test_begin_requires_draft_and_exact_summary(self):
        self.call("init", "--intent", "submit")
        self.begin(ok=False)
        self.call("checkpoint", "--stage", "draft", "--evidence", "record:verified")
        for value in ("NaN", "Infinity", "0", "-1", "1e3", "1.001", "01", " "):
            with self.subTest(amount=value):
                self.begin(ok=False, amount=value)
        self.begin(ok=False, currency="cny")
        self.begin(ok=False, **{"document-id": " "})
        self.begin(ok=False, authorization=" ")
        self.begin(ok=False, evidence=" ")
        self.assertEqual(self.call("show")["status"], "working")

    def test_pending_and_unknown_survive_restart_block_and_reject_reentry(self):
        self.draft()
        self.begin()
        self.begin(ok=False)
        state = self.call("block", "--reason", "Browser disconnected after submit.")
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["submission"]["outcome"], "unknown")
        self.call("init", "--intent", "submit")
        self.begin(ok=False)
        self.call("checkpoint", "--stage", "draft", "--evidence", "record:looked", ok=False)
        self.call("set-intent", "--intent", "draft", "--authorization", "conversation:change", ok=False)
        self.call("record-result", "--outcome", "unknown", "--evidence", "record:still-unresolved")
        self.assertEqual(self.call("show")["status"], "submission_unknown")

    def test_only_checked_not_submitted_result_releases_guard(self):
        self.draft()
        self.begin()
        state = self.call("record-result", "--outcome", "not-submitted", "--evidence", "record:oa-draft-not-sent")
        self.assertEqual(state["status"], "working")
        self.assertEqual(state["stage"], "draft")
        self.assertEqual(state["submission"]["outcome"], "not-submitted")
        self.assertEqual(self.begin()["status"], "submitting")

    def test_submitted_requires_number_and_cannot_be_reopened(self):
        self.draft()
        self.begin()
        self.call("record-result", "--outcome", "submitted", "--evidence", "record:sent-page", ok=False)
        self.assertEqual(self.call("show")["status"], "submitting")
        state = self.call("record-result", "--outcome", "submitted", "--oa-number", "OA-EXAMPLE-001",
                          "--evidence", "record:oa-number-and-status-observed")
        self.assertEqual(state["status"], "submitted")
        self.begin(ok=False)
        self.call("block", "--reason", "retry requested", ok=False)
        self.call("checkpoint", "--stage", "draft", "--evidence", "record:again", ok=False)
        self.call("set-intent", "--intent", "draft", "--authorization", "conversation:again", ok=False)
        self.call("record-result", "--outcome", "not-submitted", "--evidence", "record:again", ok=False)
        self.assertEqual(self.call("init", "--intent", "submit")["status"], "submitted")

    def test_ordinary_blocker_can_resume_with_evidence_without_stage_regression(self):
        self.draft()
        self.call("block", "--reason", "Attachment cannot be read.")
        self.begin(ok=False)
        self.call("checkpoint", "--stage", "materials", "--evidence", "record:old", ok=False)
        state = self.call("checkpoint", "--stage", "draft", "--evidence", "record:attachment-restored")
        self.assertEqual(state["status"], "working")
        self.assertIsNone(state["blocker"])
        self.assertEqual(self.begin()["status"], "submitting")

    def test_result_without_submission_is_rejected(self):
        self.draft()
        self.call("record-result", "--outcome", "not-submitted", "--evidence", "record:none", ok=False)
        self.begin()
        self.call("record-result", "--outcome", "unknown", "--oa-number", "OA-X",
                  "--evidence", "record:unclear", ok=False)

    def test_employee_and_task_records_are_separate(self):
        self.draft()
        other = self.call("init", "--intent", "draft", employee="employee-b")
        self.assertEqual(other["stage"], "started")
        another = self.call("init", "--intent", "draft", task="trip-b")
        self.assertEqual(another["stage"], "started")
        self.assertEqual(self.call("show")["stage"], "draft")

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_same_root_junction_rejected_without_is_junction_detection(self):
        target = self.root / "employee-b"
        junction = self.root / self.employee
        target.mkdir(parents=True)
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        try:
            self.assertFalse(junction.is_symlink())
            self.assertEqual(junction.resolve(), target.resolve())
            # Python 3.10/3.11 have no Path.is_junction; exercise the fallback.
            with mock.patch.object(MODULE.Path, "is_junction", return_value=False, create=True):
                with self.assertRaises(MODULE.StateError):
                    MODULE.checked_path(str(self.root), self.employee, self.task)
            self.assertEqual(list(target.iterdir()), [])
        finally:
            junction.rmdir()
        self.assertTrue(target.is_dir())

    def test_path_traversal_reserved_names_and_relative_root_are_rejected(self):
        for value in ("../other", "..", "a/b", r"a\b", "con", "lpt1", "UPPER"):
            with self.subTest(value=value):
                self.call("init", "--intent", "draft", employee=value, ok=False)
                self.call("init", "--intent", "draft", task=value, ok=False)
        self.call("init", "--intent", "draft", root="relative", ok=False)
        self.assertFalse(self.root.exists())

    def test_personal_data_cannot_be_written_inside_git_or_skill(self):
        for marker in (".git", "SKILL.md"):
            folder = self.base / marker.replace(".", "test-")
            folder.mkdir()
            (folder / marker).write_text("marker", encoding="utf-8")
            self.call("init", "--intent", "draft", root=folder / "private", ok=False)
            self.assertFalse((folder / "private").exists())

    def test_corrupt_or_foreign_state_is_not_reset(self):
        self.draft()
        original = self.state_path.read_text(encoding="utf-8")
        corrupt = json.loads(original)
        corrupt["intent"] = {"invalid": "object"}
        for content in ("{bad-json", "[]", '{"schema_version":1}', json.dumps(corrupt),
                        original.replace('"employee-a"', '"employee-b"')):
            with self.subTest(content=content[:25]):
                self.state_path.write_text(content, encoding="utf-8")
                self.call("init", "--intent", "submit", ok=False)
                self.assertEqual(self.state_path.read_text(encoding="utf-8"), content)

    def test_os_lock_prevents_concurrent_mutation_and_releases(self):
        self.draft()
        with MODULE.state_lock(self.state_path):
            self.call("block", "--reason", "concurrent attempt", ok=False)
        self.assertEqual(self.call("show")["status"], "working")
        self.call("block", "--reason", "after release")

    def test_two_processes_cannot_both_begin_submission(self):
        self.draft()
        args = self.command(
            "begin-submit", "--document-id", "draft-123", "--amount", "21.50",
            "--currency", "CNY", "--authorization", "conversation:approved",
            "--evidence", "record:checked",
        )
        processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        results = [process.communicate(timeout=20) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2], results)
        state = self.call("show")
        self.assertEqual(state["status"], "submitting")
        self.assertEqual(sum(item["action"] == "begin-submit" for item in state["events"]), 1)

    def test_failed_atomic_replace_preserves_last_checkpoint(self):
        self.draft()
        old = self.state_path.read_bytes()
        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                MODULE.atomic_write(self.state_path, {"broken": "candidate"})
        self.assertEqual(self.state_path.read_bytes(), old)
        self.assertEqual(list(self.state_path.parent.glob(".expense-*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
