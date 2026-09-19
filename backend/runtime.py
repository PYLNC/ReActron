"""SQLite-backed Runner. An in-process wake flag is never authoritative."""

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.agent import AgentStep
from backend.context import build_context
from backend.db import EventLimitExceeded, InvalidRunTransition, ResourceNotFound, db, _json
from backend.tool_gateway import ToolOutcome, execute_tool


logger = logging.getLogger("agent.runner")
MAX_CONCURRENT_RUNS = 4


class RunnerUnavailable(RuntimeError):
    code = "runner_unavailable"


def allowed_actions(run):
    if run and run["status"] == "running" and run.get("control_requested"):
        return []
    return {"queued": ["cancel"], "running": ["pause", "cancel"],
            "paused": ["resume", "cancel"]}.get(run["status"], []) if run else []


class TaskRunner:
    def __init__(self, database=db, *, model_factory=None, tool_executor=None,
                 provider_config=None, tool_context_factory=None):
        self.database = database
        self.model_factory = model_factory or AgentStep
        self.tool_executor = tool_executor or execute_tool
        self.provider_config = provider_config or self._verified_provider
        self.tool_context_factory = tool_context_factory or self._frozen_tool_context
        self._owner = f"runner-{uuid.uuid4()}"
        self._wake = asyncio.Event()
        self._task = None
        self._closing = False

    def _verified_provider(self, run):
        from backend.security import verify_frozen_provider
        from backend.services import get_runtime_config
        cfg = get_runtime_config()
        snapshot = json.loads(run["config_snapshot_json"])
        if not verify_frozen_provider(snapshot, settings=self.database.get_settings(),
                                      runtime_config=cfg):
            raise ValueError("provider_snapshot_mismatch")
        return cfg

    @staticmethod
    def _frozen_tool_context(run):
        from backend.tool_gateway import make_frozen_run_context
        return make_frozen_run_context(run)

    async def start(self):
        if self._task and not self._task.done():
            return
        self.database.recover_expired_leases()
        self._closing = False
        self._task = asyncio.create_task(self._loop(), name="reactron-runner")
        self.wake()

    async def close(self):
        self._closing = True
        self.wake()
        if self._task:
            await self._task
            self._task = None

    def wake(self):
        self._wake.set()

    async def _loop(self):
        # ponytail: four slots match P2 fan-out; add a scheduler only if polling gets hot.
        await asyncio.gather(*(self._worker_loop() for _ in range(MAX_CONCURRENT_RUNS)))

    async def _worker_loop(self):
        while not self._closing:
            try:
                self.database.recover_expired_leases()
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("Runner claim/process failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def run_once(self):
        self.database.schedule_pending_tasks()
        run = self.database.claim_next_run(self._owner, lease_seconds=120)
        if not run:
            return False
        try:
            await self._process(run)
        finally:
            self.database.schedule_pending_tasks()
        return True

    async def _fail(self, run, code):
        self.database.transition_run(
            run["id"], run["version"], "failed", "run.failed",
            {"code": code}, error={"code": code}, lease_owner=self._owner,
            lease_epoch=run["lease_epoch"], clear_deltas=True)

    def _apply_control(self, run):
        action = run["control_requested"]
        if not action:
            return False
        self.database.transition_run(
            run["id"], run["version"], "paused" if action == "pause" else "cancelled",
            "run.paused" if action == "pause" else "run.cancelled",
            {"reason": "user_request"}, lease_owner=self._owner,
            lease_epoch=run["lease_epoch"], clear_deltas=True)
        return True

    def _unresolved_effectful(self, run):
        try:
            checkpoint = json.loads(run["checkpoint_json"])
            if checkpoint.get("phase") != "tool_pending":
                return None
            assistant = self.database.get_message(checkpoint["assistant_message_id"])
            call = json.loads(assistant["tool_calls_json"])[checkpoint["next_tool_index"]]
            started = self.database.get_tool_start(run["id"], call["id"])
            if not started:
                return None
            data = json.loads(started["payload_json"])
            if data["effect"] != "effectful":
                return None
            completed = self.database._query_one("""
                SELECT 1 FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?
            """, (run["id"], call["id"]))
            return None if completed else data
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def _hold_unknown(self, run, started):
        outcome = ToolOutcome(started["call_id"], "outcome_unknown", "effectful",
                              False, "工具结果未确认", None, "unconfirmed_effect")
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.database.record_unknown_outcome(
            run["id"], run["version"], self._owner, run["lease_epoch"],
            outcome, started["name"], started["action_hash"], expires)

    async def _process(self, run):
        run_id = run["id"]
        try:
            snapshot = json.loads(run["config_snapshot_json"])
            max_rounds = max(1, min(20, int(snapshot["max_rounds"])))
            context = replace(
                self.tool_context_factory(run), **self.database.control_scope(run_id)
            )
            started = self.database._query_all(
                "SELECT payload_json FROM events WHERE run_id=? AND type='tool.started'",
                (run_id,))
            context.budget.tool_calls = len(started)
            context.budget.mcp_calls = sum(
                json.loads(row["payload_json"])["name"] in context.mcp_tools
                for row in started)
            while run["status"] == "running":
                run = self.database.get_run(run_id)
                if run["status"] != "running":
                    return
                if self._apply_control(run):
                    return
                checkpoint = json.loads(run["checkpoint_json"])
                if (checkpoint.get("v") != 1 or checkpoint.get("phase") not in
                        {"model_pending", "tool_pending"} or
                        not isinstance(checkpoint.get("round"), int) or
                        checkpoint["round"] < 1):
                    raise ValueError("invalid_checkpoint")
                if checkpoint["phase"] == "tool_pending":
                    context = replace(context, **self.database.control_scope(run_id))
                    run = await self._tool_step(run, checkpoint, context)
                    continue
                round_index = checkpoint["round"]
                if round_index > max_rounds:
                    await self._fail(run, "max_rounds_exceeded")
                    return
                cfg = self.provider_config(run)  # Must precede client construction.
                model = self.model_factory(cfg.api_key, cfg.base_url, snapshot["model"])
                context = replace(context, secrets=(cfg.api_key,))
                messages = build_context(self.database, run_id)

                async def on_delta(text):
                    for offset in range(0, len(text), 4096):
                        self.database.append_event(run_id, "model.output.delta", {
                            "round": round_index, "text": text[offset:offset + 4096]})

                result = await asyncio.wait_for(model(
                    messages, run_id=run_id, round_index=round_index,
                    tool_names=tuple(name for name, spec in context.tool_specs.items()
                                     if spec.effects <= context.grants and
                                     spec.approval == "never"),
                    on_delta=on_delta), timeout=90)
                run = self.database.get_run(run_id)
                if run["status"] != "running" or self._apply_control(run):
                    return
                calls = result.get("tool_calls") or []
                content = result.get("content")
                if len(_json({"content": content, "tool_calls": calls})) > 64_000:
                    raise ValueError("model_step_output_exceeded")
                assistant = {"role": "assistant", "content": content}
                if calls:
                    assistant["tool_calls"] = calls
                    next_checkpoint = {"v": 1, "phase": "tool_pending",
                                       "round": round_index, "assistant_message_id": None,
                                       "next_tool_index": 0}
                    run, _ = self.database.transition_run(
                        run_id, run["version"], "running", "model.completed",
                        {"round": round_index, "tool_count": len(calls)},
                        checkpoint=next_checkpoint, message=assistant,
                        lease_owner=self._owner, lease_epoch=run["lease_epoch"],
                        clear_deltas=True)
                else:
                    self.database.transition_run(
                        run_id, run["version"], "completed", "run.completed",
                        {"round": round_index}, message=assistant,
                        lease_owner=self._owner, lease_epoch=run["lease_epoch"],
                        clear_deltas=True)
                    return
        except InvalidRunTransition:
            latest = self.database.get_run(run_id)
            if (latest and latest["status"] == "running" and
                    latest["lease_owner"] == self._owner and latest["control_requested"]):
                try:
                    self._apply_control(latest)
                except InvalidRunTransition:
                    pass
            logger.info("Run CAS changed; stopping local worker: %s", run_id)
        except Exception as exc:
            logger.warning("Run failed: %s: %s", run_id, type(exc).__name__)
            latest = self.database.get_run(run_id)
            if latest and latest["status"] == "running":
                uncertain = self._unresolved_effectful(latest)
                if uncertain:
                    try:
                        self._hold_unknown(latest, uncertain)
                    except Exception:
                        # Preserve expired lease for recovery; never mislabel a side effect.
                        logger.exception("Cannot persist unknown outcome; Run held: %s", run_id)
                    return
                if latest["control_requested"]:
                    try:
                        self._apply_control(latest)
                    except InvalidRunTransition:
                        pass
                    return
                code = ("event_limit_exceeded" if isinstance(exc, EventLimitExceeded)
                        else str(exc) if str(exc) in {"provider_snapshot_mismatch",
                                                      "model_step_output_exceeded"}
                        else "runner_step_failed")
                try:
                    await self._fail(latest, code)
                except (InvalidRunTransition, EventLimitExceeded):
                    logger.exception("Run terminal commit unavailable: %s", run_id)

    async def _tool_step(self, run, checkpoint, context):
        assistant = self.database.get_message(checkpoint["assistant_message_id"])
        if not assistant or assistant["run_id"] != run["id"]:
            raise ValueError("tool_pending assistant missing")
        calls = json.loads(assistant["tool_calls_json"])
        index = checkpoint["next_tool_index"]
        if index >= len(calls):
            next_checkpoint = {"v": 1, "phase": "model_pending",
                               "round": checkpoint["round"] + 1}
            return self.database.transition_run(
                run["id"], run["version"], "running", "tool.batch_completed",
                {"round": checkpoint["round"]}, checkpoint=next_checkpoint,
                lease_owner=self._owner, lease_epoch=run["lease_epoch"])[0]
        call = calls[index]
        call_id, name = call["id"], call["function"]["name"]
        arguments = call["function"]["arguments"]
        action_hash = hashlib.sha256(_json({
            "call_id": call_id, "name": name, "arguments": arguments}).encode()).hexdigest()
        duplicate_result = self.database._query_one("""
            SELECT id FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?
            LIMIT 1
        """, (run["id"], call_id))
        if duplicate_result:
            await self._fail(run, "duplicate_tool_call_id")
            return self.database.get_run(run["id"])
        spec = context.tool_specs.get(name)
        started = self.database.get_tool_start(run["id"], call_id)
        if started and (not spec or spec.origin != "internal"):
            # Crash after the invocation marker: no approval decision is a replay grant.
            effect = json.loads(started["payload_json"])["effect"]
            if effect == "effectful":
                outcome = ToolOutcome(call_id, "outcome_unknown", effect, False,
                                      "工具结果未确认", None, "restart_unknown")
                expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                self.database.record_unknown_outcome(
                    run["id"], run["version"], self._owner, run["lease_epoch"],
                    outcome, name, action_hash, expires)
            else:
                await self._fail(run, "incomplete_tool_call")
            return self.database.get_run(run["id"])
        effect = "effectful" if spec and spec.effects - {"read_only", "filesystem.read"} else "read_only"
        cfg = self.provider_config(run)
        context = replace(context, secrets=(cfg.api_key,))
        if not started:
            run = self.database.start_tool_call(
                run["id"], run["version"], self._owner, run["lease_epoch"],
                call_id, name, effect, action_hash)
        outcome = await self.tool_executor(replace(context, call_id=call_id), name, arguments)
        if outcome.call_id != call_id:
            raise ValueError("ToolOutcome call_id mismatch")
        run = self.database.get_run(run["id"])
        if run["status"] != "running":
            return run
        if (outcome.status == "outcome_unknown" or
                (outcome.status == "timeout" and outcome.effect == "effectful"
                 and not outcome.quiesced)):
            expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            self.database.record_unknown_outcome(
                run["id"], run["version"], self._owner, run["lease_epoch"],
                outcome, name, action_hash, expires)
            return self.database.get_run(run["id"])
        if spec and spec.origin == "internal" and outcome.status == "ok":
            return self.database.apply_control_tool(
                run["id"], run["version"], self._owner, run["lease_epoch"],
                call_id, name, arguments, model_text=outcome.model_text,
            )[0]
        next_checkpoint = {**checkpoint, "next_tool_index": index + 1}
        run, _ = self.database.transition_run(
            run["id"], run["version"], "running", "tool.completed",
            {"call_id": call_id, "name": name, "status": outcome.status,
             "effect": outcome.effect, "error_code": outcome.error_code},
            checkpoint=next_checkpoint,
            message={"role": "tool", "tool_call_id": call_id,
                     "content": outcome.model_text[:8000]},
            lease_owner=self._owner, lease_epoch=run["lease_epoch"])
        return run

    async def control(self, run_id, action, expected_version=None):
        run = self.database.request_control(run_id, action, expected_version)
        self.wake()
        return run

    async def approval_decided(self, approval_id):
        approval = self.database.get_approval(approval_id)
        if not approval:
            raise ResourceNotFound("Approval 不存在")
        return self.database.get_run(approval["run_id"])

    async def wait_for_change(self, run_id, after_id, timeout=15):
        if not self.database.get_run(run_id):
            raise ResourceNotFound("Run 不存在")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            last = self.database.get_last_event_id(run_id)
            if last > after_id or asyncio.get_running_loop().time() >= deadline:
                return last
            await asyncio.sleep(min(0.2, deadline - asyncio.get_running_loop().time()))


_runner = None


def get_runner(database=db):
    global _runner
    if _runner is None or _runner.database is not database:
        _runner = TaskRunner(database)
    return _runner
