from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .models import Streamer, TimelineDocument, TimelineRevision, Vod, VodState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT,
                glossary TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS vods (
                vod_id TEXT PRIMARY KEY,
                streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                duration_text TEXT NOT NULL DEFAULT '',
                published_text TEXT NOT NULL DEFAULT '',
                thumbnail_url TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'vod',
                state TEXT NOT NULL DEFAULT 'new',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vods_streamer ON vods(streamer_id);
            CREATE INDEX IF NOT EXISTS idx_vods_state ON vods(state);

            CREATE TABLE IF NOT EXISTS timeline_documents (
                vod_id TEXT PRIMARY KEY REFERENCES vods(vod_id) ON DELETE CASCADE,
                text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'review',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS timeline_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vod_id TEXT NOT NULL REFERENCES vods(vod_id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_timeline_revisions_vod
            ON timeline_revisions(vod_id, id DESC);

            CREATE TABLE IF NOT EXISTS analysis_queue (
                vod_id TEXT PRIMARY KEY REFERENCES vods(vod_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                enqueued_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_analysis_queue_position
            ON analysis_queue(position);
            """
        )
        vod_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(vods)").fetchall()
        }
        if "source_kind" not in vod_columns:
            self.connection.execute(
                "ALTER TABLE vods ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'vod'"
            )
        streamer_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(streamers)").fetchall()
        }
        if "glossary" not in streamer_columns:
            self.connection.execute(
                "ALTER TABLE streamers ADD COLUMN glossary TEXT NOT NULL DEFAULT ''"
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def add_streamer(self, channel_id: str, display_name: str = "") -> Streamer:
        now = utc_now()
        name = display_name.strip() or channel_id
        self.connection.execute(
            """
            INSERT INTO streamers(channel_id, display_name, enabled, added_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                display_name = CASE
                    WHEN excluded.display_name = streamers.channel_id
                    THEN streamers.display_name
                    ELSE excluded.display_name
                END,
                enabled = 1
            """,
            (channel_id, name, now),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM streamers WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return self._streamer_from_row(row)

    def ensure_external_streamer(
        self,
        channel_id: str,
        display_name: str,
    ) -> Streamer:
        """Create a hidden source row without adding it to automatic discovery."""
        now = utc_now()
        name = display_name.strip() or channel_id
        self.connection.execute(
            """
            INSERT INTO streamers(channel_id, display_name, enabled, added_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                display_name = CASE
                    WHEN excluded.display_name = streamers.channel_id
                    THEN streamers.display_name
                    ELSE excluded.display_name
                END
            """,
            (channel_id, name, now),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM streamers WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return self._streamer_from_row(row)

    def remove_streamer(self, streamer_id: int) -> None:
        self.connection.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
        self.connection.commit()

    def delete_vod(self, vod_id: str) -> None:
        """Remove a single VOD and its cascaded timeline docs, revisions, queue rows."""
        self.connection.execute("DELETE FROM vods WHERE vod_id = ?", (vod_id,))
        self.connection.commit()

    def list_vod_ids_for_streamer(self, streamer_id: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT vod_id FROM vods WHERE streamer_id = ?",
            (streamer_id,),
        ).fetchall()
        return [str(row["vod_id"]) for row in rows]

    def list_streamers(self, enabled_only: bool = False) -> list[Streamer]:
        sql = "SELECT * FROM streamers"
        params: tuple[object, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = ?"
            params = (1,)
        sql += " ORDER BY display_name COLLATE NOCASE, channel_id COLLATE NOCASE"
        rows = self.connection.execute(sql, params).fetchall()
        return [self._streamer_from_row(row) for row in rows]

    def update_streamer_name(self, streamer_id: int, display_name: str) -> None:
        if not display_name.strip():
            return
        self.connection.execute(
            "UPDATE streamers SET display_name = ? WHERE id = ?",
            (display_name.strip(), streamer_id),
        )
        self.connection.commit()

    def update_streamer_glossary(self, streamer_id: int, glossary: str) -> None:
        self.connection.execute(
            "UPDATE streamers SET glossary = ? WHERE id = ?",
            (glossary.strip()[:5_000], streamer_id),
        )
        self.connection.commit()

    def record_discovery_success(self, streamer_id: int) -> None:
        self.connection.execute(
            "UPDATE streamers SET last_checked_at = ?, last_error = NULL WHERE id = ?",
            (utc_now(), streamer_id),
        )
        self.connection.commit()

    def record_discovery_error(self, streamer_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE streamers SET last_checked_at = ?, last_error = ? WHERE id = ?",
            (utc_now(), error[:500], streamer_id),
        )
        self.connection.commit()

    def upsert_discovered_vods(
        self, streamer_id: int, items: Iterable[Mapping[str, object]]
    ) -> int:
        now = utc_now()
        inserted = 0
        with self.connection:
            for item in items:
                vod_id = str(item.get("vod_id", "")).strip()
                url = str(item.get("url", "")).strip()
                title = str(item.get("title", "")).strip()
                if not vod_id or not url or not title:
                    continue
                exists = self.connection.execute(
                    "SELECT 1 FROM vods WHERE vod_id = ?", (vod_id,)
                ).fetchone()
                if exists is None:
                    inserted += 1
                self.connection.execute(
                    """
                    INSERT INTO vods(
                        vod_id, streamer_id, title, url, duration_text,
                        published_text, thumbnail_url, source_kind, state,
                        discovered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vod_id) DO UPDATE SET
                        streamer_id = excluded.streamer_id,
                        title = excluded.title,
                        url = excluded.url,
                        duration_text = excluded.duration_text,
                        published_text = excluded.published_text,
                        thumbnail_url = excluded.thumbnail_url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        vod_id,
                        streamer_id,
                        title,
                        url,
                        str(item.get("duration", "") or ""),
                        str(item.get("published", "") or ""),
                        str(item.get("thumbnail", "") or ""),
                        "vod",
                        VodState.NEW.value,
                        now,
                        now,
                    ),
                )
        return inserted

    def upsert_external_vod(
        self,
        *,
        vod_id: str,
        channel_id: str,
        streamer_name: str,
        title: str,
        url: str,
        duration_text: str = "",
        published_text: str = "",
        thumbnail_url: str = "",
        source_kind: str = "manual_vod",
        state: str = VodState.NEW.value,
    ) -> Vod:
        streamer = self.ensure_external_streamer(channel_id, streamer_name)
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO vods(
                vod_id, streamer_id, title, url, duration_text,
                published_text, thumbnail_url, source_kind, state,
                discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vod_id) DO UPDATE SET
                streamer_id = excluded.streamer_id,
                title = excluded.title,
                url = excluded.url,
                duration_text = excluded.duration_text,
                published_text = excluded.published_text,
                thumbnail_url = excluded.thumbnail_url,
                source_kind = CASE
                    WHEN vods.source_kind = 'vod' THEN vods.source_kind
                    ELSE excluded.source_kind
                END,
                updated_at = excluded.updated_at
            """,
            (
                vod_id,
                streamer.id,
                title,
                url,
                duration_text,
                published_text,
                thumbnail_url,
                source_kind,
                state,
                now,
                now,
            ),
        )
        self.connection.commit()
        result = self.get_vod(vod_id)
        if result is None:
            raise RuntimeError("수동 링크를 데이터베이스에 저장하지 못했습니다.")
        return result

    def list_vods(
        self,
        states: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[Vod]:
        params: list[object] = []
        where = ""
        if states is not None:
            state_list = list(states)
            if state_list:
                placeholders = ",".join("?" for _ in state_list)
                where = f"WHERE v.state IN ({placeholders})"
                params.extend(state_list)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT
                v.*,
                s.channel_id,
                s.display_name AS streamer_name,
                s.glossary AS streamer_glossary
            FROM vods v
            JOIN streamers s ON s.id = v.streamer_id
            {where}
            ORDER BY
                CASE WHEN v.source_kind = 'live' THEN 0 ELSE 1 END,
                v.updated_at DESC,
                CAST(v.vod_id AS INTEGER) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._vod_from_row(row) for row in rows]

    def get_vod(self, vod_id: str) -> Vod | None:
        row = self.connection.execute(
            """
            SELECT v.*, s.channel_id, s.display_name AS streamer_name,
                   s.glossary AS streamer_glossary
            FROM vods v JOIN streamers s ON s.id = v.streamer_id
            WHERE v.vod_id = ?
            """,
            (vod_id,),
        ).fetchone()
        return self._vod_from_row(row) if row else None

    def set_vod_state(self, vod_id: str, state: str) -> None:
        self.connection.execute(
            "UPDATE vods SET state = ?, updated_at = ? WHERE vod_id = ?",
            (state, utc_now(), vod_id),
        )
        self.connection.commit()

    def save_timeline(self, vod_id: str, text: str, status: str = "review") -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO timeline_documents(vod_id, text, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(vod_id) DO UPDATE SET
                text = excluded.text,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (vod_id, text, status, now),
        )
        self.connection.commit()

    def get_timeline(self, vod_id: str) -> TimelineDocument | None:
        row = self.connection.execute(
            "SELECT * FROM timeline_documents WHERE vod_id = ?", (vod_id,)
        ).fetchone()
        if row is None:
            return None
        return TimelineDocument(
            vod_id=row["vod_id"],
            text=row["text"],
            status=row["status"],
            updated_at=row["updated_at"],
        )

    def create_timeline_revision(
        self,
        vod_id: str,
        text: str,
        reason: str,
        *,
        keep: int = 50,
    ) -> int | None:
        if not text.strip():
            return None
        latest = self.connection.execute(
            "SELECT id, text FROM timeline_revisions WHERE vod_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (vod_id,),
        ).fetchone()
        if latest is not None and str(latest["text"]) == text:
            return int(latest["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO timeline_revisions(vod_id, text, reason, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (vod_id, text, reason.strip() or "수동 저장", utc_now()),
        )
        if keep > 0:
            self.connection.execute(
                """
                DELETE FROM timeline_revisions
                WHERE vod_id = ? AND id NOT IN (
                    SELECT id FROM timeline_revisions
                    WHERE vod_id = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (vod_id, vod_id, keep),
            )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_timeline_revisions(self, vod_id: str) -> list[TimelineRevision]:
        rows = self.connection.execute(
            "SELECT * FROM timeline_revisions WHERE vod_id = ? ORDER BY id DESC",
            (vod_id,),
        ).fetchall()
        return [
            TimelineRevision(
                id=int(row["id"]),
                vod_id=str(row["vod_id"]),
                text=str(row["text"]),
                reason=str(row["reason"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_timeline_revision(self, revision_id: int) -> TimelineRevision | None:
        row = self.connection.execute(
            "SELECT * FROM timeline_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return TimelineRevision(
            id=int(row["id"]),
            vod_id=str(row["vod_id"]),
            text=str(row["text"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
        )

    def enqueue_analysis(self, vod_id: str, status: str = "queued") -> None:
        now = utc_now()
        row = self.connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM analysis_queue"
        ).fetchone()
        position = int(row["next_position"] if row is not None else 1)
        self.connection.execute(
            """
            INSERT INTO analysis_queue(vod_id, position, status, enqueued_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(vod_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (vod_id, position, status, now, now),
        )
        self.connection.commit()

    def mark_analysis_running(self, vod_id: str) -> None:
        self.enqueue_analysis(vod_id, "running")

    def remove_analysis_queue(self, vod_id: str) -> None:
        self.connection.execute(
            "DELETE FROM analysis_queue WHERE vod_id = ?",
            (vod_id,),
        )
        self.connection.commit()

    def clear_analysis_queue(self) -> None:
        self.connection.execute("DELETE FROM analysis_queue")
        self.connection.commit()

    def list_analysis_queue(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT vod_id FROM analysis_queue ORDER BY position, enqueued_at"
        ).fetchall()
        return [str(row["vod_id"]) for row in rows]

    def recover_analysis_queue(self) -> list[str]:
        pending = self.list_analysis_queue()
        if not pending:
            return []
        placeholders = ",".join("?" for _ in pending)
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE analysis_queue SET status = 'queued', updated_at = ?",
                (now,),
            )
            self.connection.execute(
                f"UPDATE vods SET state = ?, updated_at = ? "
                f"WHERE vod_id IN ({placeholders})",
                (VodState.QUEUED.value, now, *pending),
            )
        return pending

    def recover_stale_live_sessions(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT vod_id FROM vods WHERE source_kind = 'live' AND state = ?",
            (VodState.ANALYZING.value,),
        ).fetchall()
        vod_ids = [str(row["vod_id"]) for row in rows]
        if vod_ids:
            placeholders = ",".join("?" for _ in vod_ids)
            now = utc_now()
            with self.connection:
                self.connection.execute(
                    f"UPDATE vods SET state = ?, updated_at = ? "
                    f"WHERE vod_id IN ({placeholders})",
                    (VodState.FAILED.value, now, *vod_ids),
                )
                self.connection.execute(
                    f"UPDATE timeline_documents SET status = ?, updated_at = ? "
                    f"WHERE vod_id IN ({placeholders})",
                    (VodState.FAILED.value, now, *vod_ids),
                )
        return vod_ids

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    @staticmethod
    def _streamer_from_row(row: sqlite3.Row) -> Streamer:
        return Streamer(
            id=row["id"],
            channel_id=row["channel_id"],
            display_name=row["display_name"],
            enabled=bool(row["enabled"]),
            added_at=row["added_at"],
            last_checked_at=row["last_checked_at"],
            last_error=row["last_error"],
            glossary=str(row["glossary"] or ""),
        )

    @staticmethod
    def _vod_from_row(row: sqlite3.Row) -> Vod:
        return Vod(
            vod_id=row["vod_id"],
            streamer_id=row["streamer_id"],
            channel_id=row["channel_id"],
            streamer_name=row["streamer_name"],
            title=row["title"],
            url=row["url"],
            duration_text=row["duration_text"],
            published_text=row["published_text"],
            thumbnail_url=row["thumbnail_url"],
            state=row["state"],
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
            source_kind=row["source_kind"],
            streamer_glossary=str(row["streamer_glossary"] or ""),
        )
