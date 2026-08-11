#!/usr/bin/env python3
"""Persistent conversations, inventory, and background AI search jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from stw_ai import (
    AiOrchestrator,
    DeterministicReasoningProvider,
    ReasoningProvider,
    StwAiTools,
)
from stw_pipeline import connect


SEARCH_STAGES = (
    "understanding_request", "resolving_constraints", "generating_legal_builds",
    "evaluating_candidates", "analyzing_uncertainty", "preparing_recommendation",
)
INVENTORY_KINDS = {"hero", "weapon", "team_perk", "gadget"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AiJobManager:
    def __init__(
        self, database: Path, asset_database: Path,
        provider: ReasoningProvider | None = None,
        *, max_parallel_jobs: int = 1,
    ):
        self.database = database.resolve()
        self.asset_database = asset_database.resolve()
        self.provider = provider or DeterministicReasoningProvider()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(max(1, max_parallel_jobs))

    def status(self) -> dict[str, Any]:
        provider_status = getattr(self.provider, "status", None)
        configured = self.asset_database.exists()
        return {
            "provider": self.provider.provider_id,
            "provider_metrics": provider_status() if provider_status else None,
            "asset_database": str(self.asset_database),
            "asset_database_ready": configured,
            "search_stages": list(SEARCH_STAGES),
        }

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = payload.get("request")
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must be a non-empty string")
        for name in ("intent", "intent_patch"):
            if payload.get(name) is not None and not isinstance(payload[name], Mapping):
                raise ValueError(f"{name} must be an object")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValueError("conversation_id must be a string")
        conversation_id = conversation_id or str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id, "conversation_id": conversation_id,
            "status": "queued", "stage": "understanding_request",
            "detail": "Waiting to start", "created_at": _now(), "updated_at": _now(),
            "progress": [], "result": None, "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._prune_jobs()
        self._persist_user_message(conversation_id, request.strip())
        thread = threading.Thread(
            target=self._run, args=(job_id, request.strip(), dict(payload)),
            name=f"stw-ai-{job_id[:8]}", daemon=True,
        )
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return json.loads(json.dumps(self._jobs[job_id]))

    def wait(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job["status"] in {"completed", "failed"}: return job
            time.sleep(0.02)
        raise TimeoutError(f"AI job {job_id} did not finish")

    def conversations(self, limit: int = 30) -> list[dict[str, Any]]:
        connection = connect(self.database)
        try:
            return [dict(row) for row in connection.execute(
                """SELECT id, title, created_at, updated_at FROM ai_conversations
                   ORDER BY updated_at DESC LIMIT ?""", (max(1, min(limit, 100)),)
            )]
        finally:
            connection.close()

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        connection = connect(self.database)
        try:
            row = connection.execute(
                "SELECT * FROM ai_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None: return None
            messages = []
            for message in connection.execute(
                """SELECT id, role, content, response_json, created_at FROM ai_messages
                   WHERE conversation_id=? ORDER BY id""", (conversation_id,)
            ):
                item = dict(message)
                item["response"] = json.loads(item.pop("response_json")) if item["response_json"] else None
                messages.append(item)
            result = dict(row)
            result["last_intent"] = json.loads(result.pop("last_intent_json")) if result["last_intent_json"] else None
            result["messages"] = messages
            return result
        finally:
            connection.close()

    def inventory(self) -> list[dict[str, Any]]:
        connection = connect(self.database)
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM ai_inventory ORDER BY entity_kind, display_name, entity_key"
            )]
        finally:
            connection.close()

    def set_inventory(self, value: Mapping[str, Any]) -> dict[str, Any]:
        kind = value.get("entity_kind")
        key = value.get("entity_key")
        display = value.get("display_name")
        owned = value.get("owned", True)
        if kind not in INVENTORY_KINDS:
            raise ValueError(f"entity_kind must be one of {sorted(INVENTORY_KINDS)}")
        if not isinstance(key, str) or not key.strip(): raise ValueError("entity_key is required")
        if not isinstance(display, str) or not display.strip(): raise ValueError("display_name is required")
        if not isinstance(owned, bool): raise ValueError("owned must be boolean")
        connection = connect(self.database)
        try:
            with connection:
                connection.execute("""
                    INSERT INTO ai_inventory(entity_kind, entity_key, display_name, owned, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(entity_kind, entity_key) DO UPDATE SET
                      display_name=excluded.display_name, owned=excluded.owned,
                      updated_at=CURRENT_TIMESTAMP
                """, (kind, key.strip(), display.strip(), int(owned)))
            return {"saved": True, "entity_kind": kind, "entity_key": key,
                    "display_name": display, "owned": owned}
        finally:
            connection.close()

    def search_catalog(self, kind: str, query: str, limit: int = 25) -> list[dict[str, Any]]:
        if not self.asset_database.exists():
            raise ValueError(f"asset catalog not found: {self.asset_database}")
        connection = connect(self.asset_database)
        try:
            return StwAiTools(connection).search_catalog(kind, query, limit)
        finally:
            connection.close()

    def _run(self, job_id: str, request: str, payload: dict[str, Any]) -> None:
        with self._semaphore:
            self._update(job_id, status="running", detail="Starting search")
            try:
                if not self.asset_database.exists():
                    raise ValueError(
                        f"asset catalog not found: {self.asset_database}; use --asset-db"
                    )
                conversation_id = self.get(job_id)["conversation_id"]
                conversation = self.conversation(conversation_id)
                messages = (conversation or {}).get("messages", [])[:-1]
                previous_intent = (conversation or {}).get("last_intent")
                patch = dict(payload.get("intent_patch") or {})
                if payload.get("restrict_to_owned") is True:
                    patch.update(self._inventory_patch())
                asset_connection = connect(self.asset_database)
                try:
                    result = AiOrchestrator(
                        StwAiTools(asset_connection), self.provider
                    ).run(
                        request, payload.get("intent"),
                        previous_intent=previous_intent,
                        intent_patch=patch,
                        conversation=messages,
                        progress=lambda stage, detail=None: self._progress(
                            job_id, stage, detail
                        ),
                    )
                finally:
                    asset_connection.close()
                self._persist_assistant_message(conversation_id, result)
                self._update(job_id, status="completed", result=result,
                             detail="Recommendation ready")
            except Exception as error:
                self._update(job_id, status="failed",
                             error=f"{type(error).__name__}: {error}",
                             detail="Search failed")

    def _inventory_patch(self) -> dict[str, Any]:
        grouped: dict[str, list[str]] = {kind: [] for kind in INVENTORY_KINDS}
        for item in self.inventory():
            if item["owned"]: grouped[item["entity_kind"]].append(item["display_name"])
        patch = {}
        mapping = {"hero": "owned_heroes", "weapon": "owned_weapons",
                   "team_perk": "owned_team_perks", "gadget": "owned_gadgets"}
        for kind, field in mapping.items():
            if grouped[kind]: patch[field] = grouped[kind]
        return patch

    def _persist_user_message(self, conversation_id: str, content: str) -> None:
        connection = connect(self.database)
        try:
            with connection:
                connection.execute("""
                    INSERT OR IGNORE INTO ai_conversations(id, title)
                    VALUES (?, ?)
                """, (conversation_id, content[:72]))
                connection.execute("""
                    INSERT INTO ai_messages(conversation_id, role, content)
                    VALUES (?, 'user', ?)
                """, (conversation_id, content))
                connection.execute(
                    "UPDATE ai_conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (conversation_id,),
                )
        finally:
            connection.close()

    def _persist_assistant_message(
        self, conversation_id: str, result: Mapping[str, Any]
    ) -> None:
        content = (
            result.get("explanation", {}).get("summary")
            or "I need more information before I can make an evidence-backed recommendation."
        )
        connection = connect(self.database)
        try:
            with connection:
                connection.execute("""
                    INSERT INTO ai_messages(conversation_id, role, content, response_json)
                    VALUES (?, 'assistant', ?, ?)
                """, (conversation_id, content,
                      json.dumps(result, ensure_ascii=False, separators=(",", ":"))))
                connection.execute("""
                    UPDATE ai_conversations SET last_intent_json=?,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?
                """, (json.dumps(result.get("intent")), conversation_id))
        finally:
            connection.close()

    def _progress(self, job_id: str, stage: str, detail: str | None) -> None:
        if stage not in SEARCH_STAGES:
            raise ValueError(f"invalid public search stage: {stage}")
        event = {"stage": stage, "detail": detail, "at": _now()}
        with self._lock:
            job = self._jobs[job_id]
            if not job["progress"] or job["progress"][-1]["stage"] != stage:
                job["progress"].append(event)
            job["stage"] = stage
            job["detail"] = detail
            job["updated_at"] = event["at"]

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)
            self._jobs[job_id]["updated_at"] = _now()

    def _prune_jobs(self) -> None:
        if len(self._jobs) <= 100: return
        completed = [key for key, value in self._jobs.items()
                     if value["status"] in {"completed", "failed"}]
        for key in completed[:len(self._jobs) - 100]: self._jobs.pop(key, None)
