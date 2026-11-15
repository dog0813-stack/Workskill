#!/usr/bin/env python3
"""Record expense-task checkpoints. This tool does not operate or verify an OA.

Evidence and authorization arguments must be references to observations/consent,
not passwords, tokens, bank details, or raw email contents. Caller assertions are
not independently verified. UI actions and deliberate state-file edits remain
outside this helper's control.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import tempfile

SCHEMA_VERSION = 1
STAGES = ("started", "materials", "draft")
STATUSES = {"working", "blocked", "submitting", "submission_unknown", "submitted"}
PENDING = {"submitting", "submission_unknown"}
PROTECTED = PENDING | {"submitted"}
RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
SKILL_DIR = Path(__file__).resolve().parents[1]


class StateError(Exception):
    """A rejected operation that must not change task state."""


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) or value in RESERVED:
        raise argparse.ArgumentTypeError(
            "Use 1-64 lowercase letters/digits, '-' or '_'; no paths or reserved names."
        )
    return value


def reference(value):
    value = value.strip()
    if not value or len(value) > 1000 or any(ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError("Provide a nonempty reference (at most 1000 characters).")
    return value


def money(value):
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?", value):
        raise argparse.ArgumentTypeError("Amount must be a decimal with at most two decimal places.")
    amount = Decimal(value)
    if amount <= 0:
        raise argparse.ArgumentTypeError("Amount must be positive.")
    return format(amount, ".2f")


def currency(value):
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise argparse.ArgumentTypeError("Use the verified three-letter uppercase currency code.")
    return value


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def checked_path(root, employee, task):
    raw = Path(root).expanduser()
    if not raw.is_absolute():
        raise StateError("--root must be an absolute private data directory.")
    root_path = raw.resolve()
    employee_path = root_path / employee
    if employee_path.is_symlink() or getattr(employee_path, "is_junction", lambda: False)():
        raise StateError("Employee directories must not be symbolic links or junctions.")
    task_dir = employee_path.resolve()
    if task_dir != employee_path:
        raise StateError("Employee directory must not redirect to a different path.")
    path = task_dir / (task + ".json")
    if path.is_symlink():
        raise StateError("Task files must not be symbolic links.")
    for directory in (task_dir, *task_dir.parents):
        if (directory / ".git").exists() or (directory / "SKILL.md").exists():
            raise StateError("Store personal task data outside every Git repository and skill directory.")
    if task_dir == SKILL_DIR or SKILL_DIR in task_dir.parents:
        raise StateError("Store personal task data outside the installed skill.")
    return path


@contextmanager
def state_lock(path):
    """OS-owned, nonblocking lock; a crashed process cannot leave a stale lock."""
    lock_path = path.with_suffix(".lock")
    if lock_path.is_symlink():
        raise StateError("Lock files must not be symbolic links.")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StateError("Another process owns this task; observe it before retrying.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(path, employee, task):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError("State is missing or unreadable; do not recreate a possibly submitted task.") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise StateError("Unsupported or malformed state; inspect the existing record.")
    if state.get("employee") != employee or state.get("task") != task:
        raise StateError("Employee/task identity differs from the existing record.")
    if (not isinstance(state.get("intent"), str) or state["intent"] not in {"draft", "submit"}
            or not isinstance(state.get("stage"), str) or state["stage"] not in STAGES
            or not isinstance(state.get("status"), str) or state["status"] not in STATUSES
            or not isinstance(state.get("events"), list)):
        raise StateError("Malformed state; no automatic reset is permitted.")
    submission = state.get("submission")
    if submission is not None:
        if not isinstance(submission, dict):
            raise StateError("Malformed submission record; reconcile with OA before continuing.")
        try:
            for key in ("document_id", "authorization", "evidence"):
                reference(submission[key])
            money(submission["amount"])
            currency(submission["currency"])
            expected_outcome = {
                "working": "not-submitted", "blocked": "not-submitted",
                "submitting": "pending", "submission_unknown": "unknown", "submitted": "submitted",
            }[state["status"]]
            if submission["outcome"] != expected_outcome:
                raise ValueError("Status conflicts with the submission outcome.")
            if state["status"] == "submitted":
                reference(submission["oa_number"])
                reference(submission["result_evidence"])
        except (KeyError, ValueError, TypeError, AttributeError, argparse.ArgumentTypeError) as exc:
            raise StateError("Incomplete/conflicting submission record; preserve it and reconcile with OA.") from exc
    elif state["status"] in PROTECTED:
        raise StateError("Submission history is missing; preserve the record and reconcile with OA.")
    return state


def atomic_write(path, state):
    """Same-directory replace: readers see either the previous or complete new JSON."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".expense-",
            suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def event(state, action, **details):
    now = timestamp()
    state["updated_at"] = now
    state["events"].append({"at": now, "action": action, **details})


def mutable_work(state):
    if state["status"] in PROTECTED:
        raise StateError("Submission is pending, unknown or complete; query OA and record its result first.")


def change(state, args):
    command = args.command
    if command == "init":
        if state["intent"] != args.intent:
            raise StateError("init cannot change intent; use set-intent with an authorization reference.")
        return False
    if command == "set-intent":
        mutable_work(state)
        state["intent"] = args.intent
        event(state, command, intent=args.intent, authorization=args.authorization)
    elif command == "checkpoint":
        mutable_work(state)
        if STAGES.index(args.stage) < STAGES.index(state["stage"]):
            raise StateError("A checkpoint cannot move a task backwards.")
        state["stage"] = args.stage
        state["status"] = "working"
        state["blocker"] = None
        event(state, command, stage=args.stage, evidence=args.evidence)
    elif command == "begin-submit":
        mutable_work(state)
        if state["status"] != "working" or state["stage"] != "draft":
            raise StateError("A verified draft checkpoint is required before submission.")
        if state["intent"] != "submit":
            raise StateError("The current intent is draft; obtain consent before changing it.")
        state["submission"] = {
            "document_id": args.document_id, "amount": args.amount,
            "currency": args.currency, "authorization": args.authorization,
            "evidence": args.evidence, "outcome": "pending",
        }
        state["status"] = "submitting"
        event(state, command, **state["submission"])
    elif command == "record-result":
        if state["status"] not in PENDING:
            raise StateError("There is no unresolved submission to reconcile.")
        if args.outcome == "submitted" and not args.oa_number:
            raise StateError("A verified OA application number is required for submitted status.")
        if args.outcome != "submitted" and args.oa_number:
            raise StateError("--oa-number is only valid for a verified submitted result.")
        state["submission"]["outcome"] = args.outcome
        state["submission"]["result_evidence"] = args.evidence
        state["submission"]["oa_number"] = args.oa_number
        state["status"] = {
            "submitted": "submitted", "unknown": "submission_unknown",
            "not-submitted": "working",
        }[args.outcome]
        state["blocker"] = None if args.outcome != "unknown" else "Submission outcome is unverified."
        event(state, command, outcome=args.outcome, oa_number=args.oa_number, evidence=args.evidence)
    elif command == "block":
        if state["status"] == "submitted":
            raise StateError("A submitted task cannot be reopened; inspect the OA application.")
        if state["status"] in PENDING:
            state["status"] = "submission_unknown"
            state["submission"]["outcome"] = "unknown"
        else:
            state["status"] = "blocked"
        state["blocker"] = args.reason
        event(state, command, reason=args.reason)
    else:
        raise StateError("Unsupported state operation.")
    return True


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, help="Absolute private data directory, outside Git/skills.")
    result.add_argument("--employee", required=True, type=identifier, help="Local employee alias, not a credential.")
    result.add_argument("--task", required=True, type=identifier, help="Stable task identifier reused when resuming.")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", help="Create or resume without clearing existing state.")
    create.add_argument("--intent", choices=("draft", "submit"), required=True)
    commands.add_parser("show", help="Read the last durable checkpoint.")
    intent = commands.add_parser("set-intent", help="Record an explicitly authorized change of intent.")
    intent.add_argument("--intent", choices=("draft", "submit"), required=True)
    intent.add_argument("--authorization", type=reference, required=True)
    checkpoint = commands.add_parser("checkpoint", help="Save observed progress or resume a normal blocker.")
    checkpoint.add_argument("--stage", choices=("materials", "draft"), required=True)
    checkpoint.add_argument("--evidence", type=reference, required=True)
    begin = commands.add_parser("begin-submit", help="Persist the guard BEFORE the single OA submit action.")
    begin.add_argument("--document-id", type=reference, required=True)
    begin.add_argument("--amount", type=money, required=True)
    begin.add_argument("--currency", type=currency, required=True)
    begin.add_argument("--authorization", type=reference, required=True)
    begin.add_argument("--evidence", type=reference, required=True)
    outcome = commands.add_parser("record-result", help="Record an outcome actually checked in OA.")
    outcome.add_argument("--outcome", choices=("submitted", "not-submitted", "unknown"), required=True)
    outcome.add_argument("--oa-number", type=reference)
    outcome.add_argument("--evidence", type=reference, required=True)
    blocker = commands.add_parser("block", help="Preserve progress and record a specific obstacle.")
    blocker.add_argument("--reason", type=reference, required=True)
    return result


def run(args):
    path = checked_path(args.root, args.employee, args.task)
    if args.command == "show":
        return path, load_state(path, args.employee, args.task)
    if args.command != "init" and not path.is_file():
        raise StateError("Task not found; reconcile prior OA work before initializing a new task.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state_lock(path):
        if path.exists():
            state = load_state(path, args.employee, args.task)
            changed = change(state, args)
        elif args.command == "init":
            state = {
                "schema_version": SCHEMA_VERSION, "employee": args.employee,
                "task": args.task, "intent": args.intent, "stage": "started",
                "status": "working", "blocker": None, "submission": None,
                "created_at": timestamp(), "events": [],
            }
            event(state, "init", intent=args.intent)
            changed = True
        else:
            raise StateError("Task disappeared; preserve evidence and inspect before continuing.")
        if changed:
            atomic_write(path, state)
    return path, state


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        path, state = run(args)
    except (StateError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "state_file": str(path), "state": state}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
