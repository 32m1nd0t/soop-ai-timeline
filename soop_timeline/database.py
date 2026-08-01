from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .models import Streamer, TimelineDocument, TimelineRevision, Vod, VodState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
                live_broadcast_no TEXT NOT NULL DEFAULT '',
                linked_vod_id TEXT NOT NULL DEFAULT '',
                memo TEXT NOT NULL DEFAULT '',
                hidden INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS live_replay_migrations (
                live_vod_id TEXT PRIMARY KEY REFERENCES vods(vod_id) ON DELETE CASCADE,
                replay_vod_id TEXT NOT NULL REFERENCES vods(vod_id) ON DELETE CASCADE,
                migrated_at TEXT NOT NULL
            );
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
        if "live_broadcast_no" not in vod_columns:
            self.connection.execute(
                "ALTER TABLE vods ADD COLUMN live_broadcast_no TEXT NOT NULL DEFAULT ''"
            )
        if "linked_vod_id" not in vod_columns:
            self.connection.execute(
                "ALTER TABLE vods ADD COLUMN linked_vod_id TEXT NOT NULL DEFAULT ''"
            )
        if "memo" not in vod_columns:
            self.connection.execute(
                "ALTER TABLE vods ADD COLUMN memo TEXT NOT NULL DEFAULT ''"
            )
        if "hidden" not in vod_columns:
            self.connection.execute(
                "ALTER TABLE vods ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
            )
        self._deduplicate_linked_replays()
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vods_unique_linked_replay
            ON vods(linked_vod_id)
            WHERE source_kind = 'live' AND linked_vod_id != ''
            """
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

    def _deduplicate_linked_replays(self) -> None:
        """Repair legacy duplicate links before enforcing one replay per live row."""

        duplicate_targets = self.connection.execute(
            """
            SELECT linked_vod_id
            FROM vods
            WHERE source_kind = 'live' AND linked_vod_id != ''
            GROUP BY linked_vod_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        now = utc_now()
        for target in duplicate_targets:
            replay_vod_id = str(target["linked_vod_id"])
            rows = self.connection.execute(
                """
                SELECT live.vod_id
                FROM vods live
                LEFT JOIN live_replay_migrations migration
                  ON migration.live_vod_id = live.vod_id
                 AND migration.replay_vod_id = live.linked_vod_id
                WHERE live.source_kind = 'live'
                  AND live.linked_vod_id = ?
                ORDER BY
                    CASE WHEN migration.live_vod_id IS NULL THEN 1 ELSE 0 END,
                    live.hidden DESC,
                    live.discovered_at ASC,
                    live.vod_id ASC
                """,
                (replay_vod_id,),
            ).fetchall()
            for duplicate in rows[1:]:
                self.connection.execute(
                    """
                    UPDATE vods
                    SET linked_vod_id = '', updated_at = ?
                    WHERE vod_id = ?
                    """,
                    (now, str(duplicate["vod_id"])),
                )

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

    def set_vod_hidden(self, vod_id: str, hidden: bool) -> None:
        self.connection.execute(
            "UPDATE vods SET hidden = ?, updated_at = ? WHERE vod_id = ?",
            (1 if hidden else 0, utc_now(), vod_id),
        )
        self.connection.commit()

    def update_vod_memo(self, vod_id: str, memo: str) -> None:
        self.connection.execute(
            "UPDATE vods SET memo = ?, updated_at = ? WHERE vod_id = ?",
            (str(memo), utc_now(), vod_id),
        )
        self.connection.commit()

    def reset_vod_work(self, vod_id: str) -> None:
        """Clear generated work while keeping the VOD visible in the main list."""
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "DELETE FROM timeline_documents WHERE vod_id = ?",
                (vod_id,),
            )
            self.connection.execute(
                "DELETE FROM timeline_revisions WHERE vod_id = ?",
                (vod_id,),
            )
            self.connection.execute(
                "DELETE FROM analysis_queue WHERE vod_id = ?",
                (vod_id,),
            )
            self.connection.execute(
                "UPDATE vods SET state = ?, updated_at = ? WHERE vod_id = ?",
                (VodState.NEW.value, now, vod_id),
            )

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

    def get_streamer(self, streamer_id: int) -> Streamer | None:
        row = self.connection.execute(
            "SELECT * FROM streamers WHERE id = ?",
            (streamer_id,),
        ).fetchone()
        return self._streamer_from_row(row) if row is not None else None

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
        live_broadcast_no: str = "",
    ) -> Vod:
        streamer = self.ensure_external_streamer(channel_id, streamer_name)
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO vods(
                vod_id, streamer_id, title, url, duration_text,
                published_text, thumbnail_url, source_kind, live_broadcast_no, state,
                discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                live_broadcast_no = CASE
                    WHEN excluded.live_broadcast_no = ''
                    THEN vods.live_broadcast_no
                    ELSE excluded.live_broadcast_no
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
                live_broadcast_no,
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
        *,
        streamer_id: int | None = None,
        sort: str = "newest",
        hidden: bool = False,
    ) -> list[Vod]:
        params: list[object] = [1 if hidden else 0]
        clauses: list[str] = [
            "v.hidden = ?",
            "NOT (v.source_kind = 'live' AND v.linked_vod_id != '')",
        ]
        if states is not None:
            state_list = list(states)
            if state_list:
                placeholders = ",".join("?" for _ in state_list)
                clauses.append(f"v.state IN ({placeholders})")
                params.extend(state_list)
        if streamer_id is not None:
            clauses.append("v.streamer_id = ?")
            params.append(streamer_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        state_rank = (
            "CASE v.state "
            "WHEN 'analyzing' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 "
            "WHEN 'review' THEN 3 WHEN 'new' THEN 4 WHEN 'ready' THEN 5 "
            "WHEN 'copied' THEN 6 WHEN 'published' THEN 7 ELSE 8 END"
        )
        duration_text = "TRIM(REPLACE(v.duration_text, '시작 ', ''))"
        duration_seconds = (
            "CASE "
            f"WHEN {duration_text} GLOB '*:*:*' THEN "
            f"CAST(SUBSTR({duration_text}, 1, INSTR({duration_text}, ':') - 1) AS INTEGER) * 3600 + "
            f"CAST(SUBSTR({duration_text}, INSTR({duration_text}, ':') + 1, 2) AS INTEGER) * 60 + "
            f"CAST(SUBSTR({duration_text}, -2) AS INTEGER) "
            f"WHEN {duration_text} GLOB '*:*' THEN "
            f"CAST(SUBSTR({duration_text}, 1, INSTR({duration_text}, ':') - 1) AS INTEGER) * 60 + "
            f"CAST(SUBSTR({duration_text}, -2) AS INTEGER) "
            "ELSE 0 END"
        )
        numeric_vod_id = (
            "CASE WHEN v.vod_id GLOB '[0-9]*' "
            "THEN CAST(v.vod_id AS INTEGER) END"
        )
        newest = (
            "CASE WHEN v.source_kind = 'live' THEN 0 ELSE 1 END, "
            f"{numeric_vod_id} DESC, "
            "v.discovered_at DESC"
        )
        oldest = (
            "CASE WHEN v.source_kind = 'live' THEN 1 ELSE 0 END, "
            f"{numeric_vod_id} ASC, "
            "v.discovered_at ASC"
        )
        order_by = {
            "newest": newest,
            "oldest": oldest,
            "recent_work": "v.updated_at DESC, v.discovered_at DESC",
            "status": (
                f"{state_rank}, v.updated_at DESC"
            ),
            "state_asc": f"{state_rank} ASC, v.updated_at DESC",
            "state_desc": f"{state_rank} DESC, v.updated_at DESC",
            "streamer_asc": (
                "s.display_name COLLATE NOCASE ASC, v.title COLLATE NOCASE ASC"
            ),
            "streamer_desc": (
                "s.display_name COLLATE NOCASE DESC, v.title COLLATE NOCASE ASC"
            ),
            "title_asc": (
                "v.title COLLATE NOCASE ASC, s.display_name COLLATE NOCASE ASC"
            ),
            "title_desc": (
                "v.title COLLATE NOCASE DESC, s.display_name COLLATE NOCASE ASC"
            ),
            "memo_asc": "v.memo COLLATE NOCASE ASC, v.updated_at DESC",
            "memo_desc": "v.memo COLLATE NOCASE DESC, v.updated_at DESC",
            "duration_asc": f"{duration_seconds} ASC, {newest}",
            "duration_desc": f"{duration_seconds} DESC, {newest}",
            "published_asc": oldest,
            "published_desc": newest,
            "vod_id_asc": (
                "CASE WHEN v.vod_id GLOB '[0-9]*' THEN 0 ELSE 1 END, "
                f"{numeric_vod_id} ASC, v.vod_id COLLATE NOCASE ASC"
            ),
            "vod_id_desc": (
                "CASE WHEN v.vod_id GLOB '[0-9]*' THEN 0 ELSE 1 END, "
                f"{numeric_vod_id} DESC, v.vod_id COLLATE NOCASE DESC"
            ),
        }.get(sort, "")
        if not order_by:
            raise ValueError(f"지원하지 않는 VOD 정렬 방식입니다: {sort}")
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
            ORDER BY {order_by}
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._vod_from_row(row) for row in rows]

    def auto_link_live_sessions(
        self,
        streamer_id: int,
        vod_ids: Iterable[str],
        *,
        new_vod_ids: Iterable[str] = (),
    ) -> list[tuple[str, str]]:
        """Attach recent live work sessions to their completed replay VODs."""
        requested = [str(vod_id) for vod_id in vod_ids if str(vod_id).isdigit()]
        if not requested:
            return []
        placeholders = ",".join("?" for _ in requested)
        replays = self.connection.execute(
            f"""
            SELECT vod_id, title, live_broadcast_no
            FROM vods
            WHERE streamer_id = ?
              AND source_kind != 'live'
              AND vod_id IN ({placeholders})
            ORDER BY CAST(vod_id AS INTEGER) DESC
            """,
            (streamer_id, *requested),
        ).fetchall()
        if not replays:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(
            timespec="seconds"
        )
        candidates = self.connection.execute(
            """
            SELECT vod_id, title, live_broadcast_no, discovered_at
            FROM vods
            WHERE streamer_id = ?
              AND source_kind = 'live'
              AND linked_vod_id = ''
              AND state != ?
              AND discovered_at >= ?
            ORDER BY discovered_at DESC
            """,
            (streamer_id, VodState.ANALYZING.value, cutoff),
        ).fetchall()
        if not candidates:
            return []

        fresh_ids = {str(vod_id) for vod_id in new_vod_ids}
        available = list(candidates)
        linked: list[tuple[str, str]] = []
        used_replays = {
            str(row["linked_vod_id"])
            for row in self.connection.execute(
                "SELECT linked_vod_id FROM vods WHERE linked_vod_id != ''"
            ).fetchall()
        }
        for replay in replays:
            replay_id = str(replay["vod_id"])
            if replay_id in used_replays:
                continue
            replay_title = _normalized_broadcast_title(str(replay["title"]))
            replay_broadcast_no = str(replay["live_broadcast_no"] or "")
            matches = []
            if replay_broadcast_no:
                matches = [
                    candidate
                    for candidate in available
                    if str(candidate["live_broadcast_no"] or "")
                    == replay_broadcast_no
                ]
            if not matches:
                matches = [
                    candidate
                    for candidate in available
                    if replay_title
                    and _normalized_broadcast_title(str(candidate["title"]))
                    == replay_title
                ]
            if not matches and len(fresh_ids) == 1 and replay_id in fresh_ids and len(available) == 1:
                matches = [available[0]]
            if not matches:
                continue
            live = matches[0]
            live_id = str(live["vod_id"])
            self.connection.execute(
                "UPDATE vods SET linked_vod_id = ?, updated_at = ? WHERE vod_id = ?",
                (replay_id, utc_now(), live_id),
            )
            linked.append((live_id, replay_id))
            used_replays.add(replay_id)
            available.remove(live)
        self.connection.commit()
        return linked

    def link_live_session_to_replay(
        self,
        live_vod_id: str,
        replay_vod_id: str,
    ) -> None:
        """Explicitly link a live work session to the replay selected by the user."""
        live = self.connection.execute(
            """
            SELECT vod_id, streamer_id, source_kind, linked_vod_id
            FROM vods
            WHERE vod_id = ?
            """,
            (live_vod_id,),
        ).fetchone()
        replay = self.connection.execute(
            """
            SELECT vod_id, streamer_id, source_kind
            FROM vods
            WHERE vod_id = ?
            """,
            (replay_vod_id,),
        ).fetchone()
        if live is None or replay is None:
            raise ValueError("라이브 세션 또는 다시보기 기록을 찾지 못했습니다.")
        if str(live["source_kind"]) != "live":
            raise ValueError("연결 대상이 라이브 분석 기록이 아닙니다.")
        if str(replay["source_kind"]) == "live":
            raise ValueError("라이브 주소가 아니라 완성된 다시보기를 연결해야 합니다.")
        if int(live["streamer_id"]) != int(replay["streamer_id"]):
            raise ValueError("같은 스트리머의 다시보기만 연결할 수 있습니다.")

        current = str(live["linked_vod_id"] or "")
        if current:
            if current == replay_vod_id:
                return
            current_exists = self.connection.execute(
                "SELECT 1 FROM vods WHERE vod_id = ?",
                (current,),
            ).fetchone()
            if current_exists is not None:
                raise ValueError("이미 다른 다시보기가 연결된 라이브 기록입니다.")

        duplicate = self.connection.execute(
            """
            SELECT vod_id
            FROM vods
            WHERE source_kind = 'live'
              AND linked_vod_id = ?
              AND vod_id != ?
            LIMIT 1
            """,
            (replay_vod_id, live_vod_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("이 다시보기는 다른 라이브 분석 기록에 이미 연결되어 있습니다.")

        try:
            with self.connection:
                updated = self.connection.execute(
                    """
                    UPDATE vods
                    SET linked_vod_id = ?, updated_at = ?
                    WHERE vod_id = ?
                      AND source_kind = 'live'
                      AND linked_vod_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM vods other
                          WHERE other.source_kind = 'live'
                            AND other.linked_vod_id = ?
                            AND other.vod_id != ?
                      )
                    """,
                    (
                        replay_vod_id,
                        utc_now(),
                        live_vod_id,
                        current,
                        replay_vod_id,
                        live_vod_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "이 다시보기는 다른 라이브 분석 기록에 이미 연결되어 있습니다."
                    )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "이 다시보기는 다른 라이브 분석 기록에 이미 연결되어 있습니다."
            ) from error

    def list_recent_unlinked_live_sessions(self, *, days: int = 2) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat(
            timespec="seconds"
        )
        rows = self.connection.execute(
            """
            SELECT vod_id FROM vods
            WHERE source_kind = 'live' AND linked_vod_id = '' AND discovered_at >= ?
            ORDER BY discovered_at DESC
            """,
            (cutoff,),
        ).fetchall()
        return [str(row["vod_id"]) for row in rows]

    def list_pending_live_replay_migrations(self) -> list[tuple[str, str]]:
        """Return linked live sessions whose user work has not moved yet."""

        rows = self.connection.execute(
            """
            SELECT live.vod_id AS live_vod_id, live.linked_vod_id AS replay_vod_id
            FROM vods live
            JOIN vods replay ON replay.vod_id = live.linked_vod_id
            LEFT JOIN live_replay_migrations migration
              ON migration.live_vod_id = live.vod_id
            WHERE live.source_kind = 'live'
              AND live.linked_vod_id != ''
              AND replay.source_kind != 'live'
              AND migration.live_vod_id IS NULL
            ORDER BY live.discovered_at ASC
            """
        ).fetchall()
        return [
            (str(row["live_vod_id"]), str(row["replay_vod_id"]))
            for row in rows
        ]

    def migrate_live_session_work(
        self,
        live_vod_id: str,
        replay_vod_id: str,
    ) -> bool:
        """Move live timeline work onto its replay while retaining live cache metadata."""

        live = self.connection.execute(
            """
            SELECT vod_id, streamer_id, source_kind, linked_vod_id,
                   live_broadcast_no, memo, state
            FROM vods WHERE vod_id = ?
            """,
            (live_vod_id,),
        ).fetchone()
        replay = self.connection.execute(
            """
            SELECT vod_id, streamer_id, source_kind, memo, state
            FROM vods WHERE vod_id = ?
            """,
            (replay_vod_id,),
        ).fetchone()
        if live is None or replay is None:
            raise ValueError("라이브 세션 또는 다시보기 기록을 찾지 못했습니다.")
        if str(live["source_kind"]) != "live":
            raise ValueError("이전 대상이 라이브 분석 기록이 아닙니다.")
        if str(replay["source_kind"]) == "live":
            raise ValueError("라이브 작업은 완성된 다시보기로만 이전할 수 있습니다.")
        if int(live["streamer_id"]) != int(replay["streamer_id"]):
            raise ValueError("같은 스트리머의 다시보기로만 작업을 이전할 수 있습니다.")
        if str(live["linked_vod_id"] or "") != replay_vod_id:
            raise ValueError("라이브 기록에 연결된 다시보기가 일치하지 않습니다.")

        migrated = self.connection.execute(
            """
            SELECT replay_vod_id FROM live_replay_migrations
            WHERE live_vod_id = ?
            """,
            (live_vod_id,),
        ).fetchone()
        if migrated is not None:
            if str(migrated["replay_vod_id"]) != replay_vod_id:
                raise ValueError("라이브 작업이 이미 다른 다시보기로 이전되었습니다.")
            return False

        live_document = self.connection.execute(
            "SELECT text, status, updated_at FROM timeline_documents WHERE vod_id = ?",
            (live_vod_id,),
        ).fetchone()
        replay_document = self.connection.execute(
            "SELECT text, status, updated_at FROM timeline_documents WHERE vod_id = ?",
            (replay_vod_id,),
        ).fetchone()
        replay_snapshot = self.connection.execute(
            """
            SELECT text, created_at FROM timeline_revisions
            WHERE vod_id = ? ORDER BY id DESC LIMIT 1
            """,
            (replay_vod_id,),
        ).fetchone()
        now = utc_now()
        live_text = str(live_document["text"]) if live_document is not None else ""
        replay_text = (
            str(replay_document["text"]) if replay_document is not None else ""
        )
        preserved_states = {
            VodState.READY.value,
            VodState.COPIED.value,
            VodState.PUBLISHED.value,
            VodState.SKIPPED.value,
        }
        migrated_state = (
            str(live["state"])
            if str(live["state"]) in preserved_states
            else VodState.REVIEW.value
        )
        replay_state = str(replay["state"])
        replay_document_status = (
            str(replay_document["status"]) if replay_document is not None else ""
        )
        replay_is_completed = (
            replay_state in preserved_states
            or replay_document_status in preserved_states
        )
        live_updated_at = (
            str(live_document["updated_at"]) if live_document is not None else ""
        )
        replay_updated_at = (
            str(replay_document["updated_at"])
            if replay_document is not None
            else ""
        )
        replay_is_newer = bool(
            live_updated_at
            and replay_updated_at
            and replay_updated_at > live_updated_at
        )
        replay_changed_after_snapshot = bool(
            replay_snapshot is not None
            and replay_text != str(replay_snapshot["text"])
            and replay_updated_at >= str(replay_snapshot["created_at"])
        )
        keep_replay_document = bool(
            replay_text.strip()
            and (
                replay_is_completed
                or replay_is_newer
                or replay_changed_after_snapshot
            )
        )
        if replay_state in preserved_states:
            retained_replay_state = replay_state
        elif replay_document_status in preserved_states:
            retained_replay_state = replay_document_status
        else:
            retained_replay_state = VodState.REVIEW.value

        replay_memo = str(replay["memo"] or "").strip()
        live_memo = str(live["memo"] or "").strip()
        if not replay_memo:
            merged_memo = live_memo
        elif live_memo and live_memo != replay_memo:
            merged_memo = f"{replay_memo}\n\n[라이브 작업에서 이전]\n{live_memo}"
        else:
            merged_memo = replay_memo

        with self.connection:
            if replay_text.strip() and replay_text != live_text:
                self.connection.execute(
                    """
                    INSERT INTO timeline_revisions(vod_id, text, reason, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        replay_vod_id,
                        replay_text,
                        "다시보기 기존 작업 · 라이브 통합 전",
                        now,
                    ),
                )

            self.connection.execute(
                "UPDATE timeline_revisions SET vod_id = ? WHERE vod_id = ?",
                (replay_vod_id, live_vod_id),
            )

            if live_text.strip():
                latest = self.connection.execute(
                    """
                    SELECT text FROM timeline_revisions
                    WHERE vod_id = ? ORDER BY id DESC LIMIT 1
                    """,
                    (replay_vod_id,),
                ).fetchone()
                if latest is None or str(latest["text"]) != live_text:
                    self.connection.execute(
                        """
                        INSERT INTO timeline_revisions(vod_id, text, reason, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            replay_vod_id,
                            live_text,
                            "라이브 작업에서 이전",
                            now,
                        ),
                    )
                if not keep_replay_document:
                    self.connection.execute(
                        """
                        INSERT INTO timeline_documents(vod_id, text, status, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(vod_id) DO UPDATE SET
                            text = excluded.text,
                            status = excluded.status,
                            updated_at = excluded.updated_at
                        """,
                        (replay_vod_id, live_text, migrated_state, now),
                    )
                self.connection.execute(
                    """
                    UPDATE vods
                    SET state = ?, memo = ?,
                        live_broadcast_no = CASE
                            WHEN live_broadcast_no = '' THEN ?
                            ELSE live_broadcast_no
                        END,
                        updated_at = ?
                    WHERE vod_id = ?
                    """,
                    (
                        (
                            retained_replay_state
                            if keep_replay_document
                            else migrated_state
                        ),
                        merged_memo,
                        str(live["live_broadcast_no"] or ""),
                        now,
                        replay_vod_id,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE vods
                    SET memo = ?,
                        live_broadcast_no = CASE
                            WHEN live_broadcast_no = '' THEN ?
                            ELSE live_broadcast_no
                        END,
                        updated_at = ?
                    WHERE vod_id = ?
                    """,
                    (
                        merged_memo,
                        str(live["live_broadcast_no"] or ""),
                        now,
                        replay_vod_id,
                    ),
                )

            self.connection.execute(
                "DELETE FROM analysis_queue WHERE vod_id = ?",
                (live_vod_id,),
            )
            self.connection.execute(
                "DELETE FROM timeline_documents WHERE vod_id = ?",
                (live_vod_id,),
            )
            self.connection.execute(
                "UPDATE vods SET hidden = 1, updated_at = ? WHERE vod_id = ?",
                (now, live_vod_id),
            )
            self.connection.execute(
                """
                DELETE FROM timeline_revisions
                WHERE vod_id = ? AND id NOT IN (
                    SELECT id FROM timeline_revisions
                    WHERE vod_id = ? ORDER BY id DESC LIMIT 50
                )
                """,
                (replay_vod_id, replay_vod_id),
            )
            self.connection.execute(
                """
                INSERT INTO live_replay_migrations(
                    live_vod_id, replay_vod_id, migrated_at
                ) VALUES (?, ?, ?)
                """,
                (live_vod_id, replay_vod_id, now),
            )
        return True

    def list_live_sessions_for_broadcast(
        self,
        streamer_id: int,
        broadcast_no: str,
    ) -> list[Vod]:
        """Return every saved capture belonging to one exact SOOP broadcast."""

        value = str(broadcast_no).strip()
        if not value:
            return []
        rows = self.connection.execute(
            """
            SELECT v.*, s.channel_id, s.display_name AS streamer_name,
                   s.glossary AS streamer_glossary
            FROM vods v
            JOIN streamers s ON s.id = v.streamer_id
            WHERE v.streamer_id = ?
              AND v.source_kind = 'live'
              AND v.live_broadcast_no = ?
            ORDER BY v.discovered_at ASC
            """,
            (streamer_id, value),
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
            WHERE timeline_documents.text != excluded.text
               OR timeline_documents.status != excluded.status
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
        """Return interrupted live sessions without discarding resumable state.

        Live transcript journals are append-only and can be continued after an
        application restart.  Keeping both the VOD and document in ``analyzing``
        distinguishes an application shutdown from an intentional live stop,
        which moves the session to ``review``.
        """
        rows = self.connection.execute(
            """
            SELECT vod_id FROM vods
            WHERE source_kind = 'live'
              AND state = ?
              AND hidden = 0
              AND linked_vod_id = ''
            """,
            (VodState.ANALYZING.value,),
        ).fetchall()
        return [str(row["vod_id"]) for row in rows]

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
            live_broadcast_no=str(row["live_broadcast_no"] or ""),
            linked_vod_id=str(row["linked_vod_id"] or ""),
            memo=str(row["memo"] or ""),
            hidden=bool(row["hidden"]),
        )


def _normalized_broadcast_title(value: str) -> str:
    text = re.sub(r"^\s*\[?\s*live\s*\]?\s*", "", value, flags=re.IGNORECASE)
    text = re.sub(r"(?:다시보기|풀영상|full\s*vod)", "", text, flags=re.IGNORECASE)
    return "".join(character.casefold() for character in text if character.isalnum())
