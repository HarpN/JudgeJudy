from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def get_db_path() -> Path:
    return Path(os.getenv("JUDY_DB_PATH", "judy.db"))


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(str(get_db_path()))
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS local_backlog (
                entity_id TEXT PRIMARY KEY,
                game_title TEXT NOT NULL,
                release_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                current_completion INTEGER NOT NULL DEFAULT 0,
                non_dlc_trophies_total INTEGER NOT NULL DEFAULT 0,
                non_dlc_trophies_earned INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                council_id TEXT NOT NULL,
                target_table TEXT NOT NULL,
                action_type TEXT NOT NULL,
                verdict TEXT NOT NULL,
                rationale TEXT NOT NULL,
                system_action TEXT NOT NULL DEFAULT 'UNKNOWN',
                human_review_required INTEGER NOT NULL DEFAULT 0,
                votes_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                target_council_id TEXT NOT NULL,
                requested_verdict TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                council_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                rationale TEXT NOT NULL,
                system_action TEXT NOT NULL,
                human_review_required INTEGER NOT NULL DEFAULT 0,
                votes_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL
            )
            """
        )
        ensure_audit_log_columns(connection)
        seed_sample_rows(connection)


def seed_sample_rows(connection: sqlite3.Connection) -> None:
    count = connection.execute("SELECT COUNT(*) AS total FROM local_backlog").fetchone()["total"]
    if count:
        return

    connection.execute(
        """
        INSERT INTO local_backlog (
            entity_id,
            game_title,
            release_date,
            status,
            current_completion,
            non_dlc_trophies_total,
            non_dlc_trophies_earned,
            notes,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "game_105",
            "Astro Bot",
            "2024-09-06",
            "ACTIVE",
            78,
            45,
            35,
            "Sample portfolio record seeded on startup.",
            utc_now_iso(),
        ),
    )


def ensure_audit_log_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(audit_logs)").fetchall()
    }
    if "system_action" not in existing_columns:
        connection.execute("ALTER TABLE audit_logs ADD COLUMN system_action TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if "human_review_required" not in existing_columns:
        connection.execute("ALTER TABLE audit_logs ADD COLUMN human_review_required INTEGER NOT NULL DEFAULT 0")
    if "votes_json" not in existing_columns:
        connection.execute("ALTER TABLE audit_logs ADD COLUMN votes_json TEXT NOT NULL DEFAULT '[]'")


def list_backlog() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM local_backlog ORDER BY updated_at DESC, game_title ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_backlog_entity(entity_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM local_backlog WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        return dict(row) if row else None


def get_audit_log_entry(council_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM audit_logs WHERE council_id = ? ORDER BY id DESC LIMIT 1",
            (council_id,),
        ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        entry["votes"] = json.loads(entry.pop("votes_json") or "[]")
        entry["payload"] = json.loads(entry.pop("payload_json") or "{}")
        entry["human_review_required"] = bool(entry.get("human_review_required", 0))
        return entry


def update_backlog_entity(entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_backlog_entity(entity_id)
    if current is None:
        raise KeyError(f"Entity not found: {entity_id}")

    status = payload.get("status", current["status"])
    completion = int(payload.get("completion", current["current_completion"]))
    notes = payload.get("notes", current["notes"])
    non_dlc_earned = int(payload.get("non_dlc_trophies_earned", current["non_dlc_trophies_earned"]))
    non_dlc_total = int(payload.get("non_dlc_trophies_total", current["non_dlc_trophies_total"]))

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE local_backlog
            SET status = ?,
                current_completion = ?,
                non_dlc_trophies_earned = ?,
                non_dlc_trophies_total = ?,
                notes = ?,
                updated_at = ?
            WHERE entity_id = ?
            """,
            (
                status,
                completion,
                non_dlc_earned,
                non_dlc_total,
                notes,
                utc_now_iso(),
                entity_id,
            ),
        )

    updated = get_backlog_entity(entity_id)
    if updated is None:
        raise RuntimeError("Entity update failed unexpectedly.")
    return updated


def append_audit_log(
    *,
    correlation_id: str,
    agent_id: str,
    council_id: str,
    target_table: str,
    action_type: str,
    verdict: str,
    rationale: str,
    system_action: str,
    human_review_required: bool,
    votes: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_logs (
                created_at,
                correlation_id,
                agent_id,
                council_id,
                target_table,
                action_type,
                verdict,
                rationale,
                system_action,
                human_review_required,
                votes_json,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                correlation_id,
                agent_id,
                council_id,
                target_table,
                action_type,
                verdict,
                rationale,
                system_action,
                int(human_review_required),
                json.dumps(votes, separators=(",", ":"), sort_keys=True),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )


def insert_review_action(
    *,
    actor_name: str,
    actor_type: str,
    action_kind: str,
    target_council_id: str,
    requested_verdict: str,
    note: str,
    council_id: str,
    verdict: str,
    rationale: str,
    system_action: str,
    human_review_required: bool,
    votes: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO review_actions (
                created_at,
                actor_name,
                actor_type,
                action_kind,
                target_council_id,
                requested_verdict,
                note,
                council_id,
                verdict,
                rationale,
                system_action,
                human_review_required,
                votes_json,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                actor_name,
                actor_type,
                action_kind,
                target_council_id,
                requested_verdict,
                note,
                council_id,
                verdict,
                rationale,
                system_action,
                int(human_review_required),
                json.dumps(votes, separators=(",", ":"), sort_keys=True),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )


def list_audit_logs(limit: int = 50) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        logs = []
        for row in rows:
            entry = dict(row)
            entry["votes"] = json.loads(entry.pop("votes_json") or "[]")
            entry["payload"] = json.loads(entry.pop("payload_json") or "{}")
            entry["human_review_required"] = bool(entry["human_review_required"])
            logs.append(entry)
        return logs


def list_review_actions(limit: int = 25) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM review_actions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        actions = []
        for row in rows:
            entry = dict(row)
            entry["votes"] = json.loads(entry.pop("votes_json") or "[]")
            entry["payload"] = json.loads(entry.pop("payload_json") or "{}")
            entry["human_review_required"] = bool(entry["human_review_required"])
            actions.append(entry)
        return actions
