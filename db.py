"""SQLite 封装（里程碑 M0，v0.4 任务制）。

严格按文档 §6 的 DDL 建 7 张表：songs（带 task_id）/ tasks / search_cache /
uploader_cache / whitelist_bv / credentials / kv_meta。

设计要点：
- 任务制（文档 §4.4）：每次 run 生成唯一 task_id，songs 以 (song_key, task_id)
  为联合主键隔离任务数据；tasks 表记录任务生命周期（RUNNING/DONE/FAILED）；
- search_cache 以关键词为 key，跨任务共享（缓存与 task_id 无关）；
- 每次状态机跃迁通过 upsert_song 立即落盘（文档 §4.4 断点续跑）；
  转 DONE 时 fail_reason 强制置 NULL（文档 §6 状态跃迁约束：成功与失败原因互斥）；
- execute / query 封装统一提交语义，单连接 + check_same_thread=False
  以兼容 asyncio 并发下的共享使用。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

# 文档 §6 的建表 DDL（v0.4 任务制：songs 加 task_id 联合主键，新增 tasks 表）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS songs (
  song_key     TEXT,               -- "歌名|歌手"
  task_id      TEXT,               -- 任务标识（文档 §6，与 song_key 联合主键）
  ncm_id       INTEGER,
  name         TEXT,
  artist       TEXT,
  album        TEXT,
  alia         TEXT,               -- JSON 数组
  origin       TEXT,               -- 翻唱原曲信息 JSON
  status       TEXT,               -- PENDING/MATCHED/DONE/MANUAL/FAV_FAILED（MATCHED=已匹配待收藏；与文档 §3/§4.1/§6 三处一致）
  method       TEXT,               -- MANUAL/WHITELIST_BV/UPLOADER_WL/SCORED
  bvid         TEXT,
  score_detail TEXT,               -- 评分明细 JSON（抽查调权重用）
  fail_reason  TEXT,
  updated_at   INTEGER,
  PRIMARY KEY (song_key, task_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,    -- 唯一任务标识（UUID）
  playlist_id TEXT,                -- 关联的网易云歌单 ID
  status      TEXT,                -- RUNNING/DONE/FAILED
  created_at  INTEGER,             -- 创建时间戳
  finished_at INTEGER,             -- 结束时间戳（未完成时 NULL）
  stats       TEXT                 -- 统计 JSON（total/done/manual，正式任务含 fav 收藏汇总）
);

CREATE TABLE IF NOT EXISTS search_cache (
  keyword    TEXT PRIMARY KEY,
  results    TEXT,                 -- 候选 JSON
  fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS uploader_cache (
  mid       INTEGER PRIMARY KEY,
  followers INTEGER,
  fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS whitelist_bv (
  song_key TEXT PRIMARY KEY,
  bvid     TEXT,
  source   TEXT                    -- whitelist / manual
);

CREATE TABLE IF NOT EXISTS credentials (
  key        TEXT PRIMARY KEY,     -- 'bili_cookie'
  value      TEXT,                 -- Fernet 加密后的 cookie 串
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS kv_meta (
  key        TEXT PRIMARY KEY,     -- 如 wbi_keys
  value      TEXT,
  fetched_at INTEGER
);
"""

# 必须存在的 7 张表（文档 §6）
EXPECTED_TABLES = frozenset(
    {
        "songs", "tasks", "search_cache", "uploader_cache",
        "whitelist_bv", "credentials", "kv_meta",
    }
)


class Database:
    """SQLite 连接与常用操作封装。

    Args:
        path: 数据库文件路径（测试可用 tmp_path；":memory:" 也可）。
    """

    def __init__(self, path: str | Path = "cache.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        self._migrate()

    def _create_tables(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def _migrate(self) -> None:
        """v0.3 → v0.4 迁移：老 songs 表无 task_id 列时重建为联合主键。

        保留已有歌曲数据并归入 task_id='legacy'（历史任务），避免破坏旧库。
        迁移完成后统一补建 task_id 索引（新库在 SCHEMA_SQL 中不建索引，
        避免旧表迁移前 CREATE INDEX 因缺列报错）。

        v0.4.4 迁移（F2-1，文档 §6 迁移注记 / §4.4）：老库（v0.4.2 及之前，
        无 MATCHED 语义）songs 全部 DONE 行一次性转 MATCHED，交阶段三重新
        收藏——deal 对已收藏返回 code 0 幂等，宁可重复收藏、不可漏收藏。
        经 kv_meta 标记保证仅执行一次：此后 fav_one() 写入的 DONE 不再被翻转。
        """
        cols = [r["name"] for r in self.query("PRAGMA table_info(songs)")]
        if "task_id" not in cols:
            with self._conn:
                self._conn.executescript(
                    """
                    ALTER TABLE songs RENAME TO songs_legacy;
                    CREATE TABLE songs (
                      song_key     TEXT,
                      task_id      TEXT,
                      ncm_id       INTEGER,
                      name         TEXT,
                      artist       TEXT,
                      album        TEXT,
                      alia         TEXT,
                      origin       TEXT,
                      status       TEXT,
                      method       TEXT,
                      bvid         TEXT,
                      score_detail TEXT,
                      fail_reason  TEXT,
                      updated_at   INTEGER,
                      PRIMARY KEY (song_key, task_id)
                    );
                    INSERT INTO songs (song_key, task_id, ncm_id, name, artist, album,
                                       alia, origin, status, method, bvid, score_detail,
                                       fail_reason, updated_at)
                    SELECT song_key, 'legacy', ncm_id, name, artist, album, alia, origin,
                           status, method, bvid, score_detail, fail_reason, updated_at
                    FROM songs_legacy;
                    DROP TABLE songs_legacy;
                    """
                )
        # 统一补建 task_id 索引（任务查询/删除走该列）
        self.execute("CREATE INDEX IF NOT EXISTS idx_songs_task_id ON songs (task_id)")
        # F2-1（§4.4/§6）：老库全 DONE → MATCHED，一次性（kv_meta 标记防重复迁移）
        marker = self.query_one(
            "SELECT value FROM kv_meta WHERE key = 'migrated_done_to_matched_v044'"
        )
        if marker is None:
            self.execute("UPDATE songs SET status = 'MATCHED' WHERE status = 'DONE'")
            self.execute(
                "INSERT INTO kv_meta (key, value, fetched_at) "
                "VALUES ('migrated_done_to_matched_v044', '1', ?)",
                (int(time.time()),),
            )

    # ---- 基础封装 -------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """执行单条写/DDL 语句并立即提交（每次状态跃迁落盘语义）。"""
        with self._conn:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        with self._conn:
            self._conn.executemany(sql, seq_of_params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """查询全部结果行（sqlite3.Row，支持按列名取值）。"""
        cur = self._conn.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, params)
        return cur.fetchone()

    def table_names(self) -> set[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return {row["name"] for row in rows}

    # ---- 业务方法 -------------------------------------------------

    def upsert_song(self, song_key: str, **fields: Any) -> None:
        """写入/更新一首歌的状态，立即落盘（文档 §4.4）。

        Args:
            song_key: "歌名|歌手"。
            fields: 任意可更新字段（task_id/ncm_id/name/artist/status/method/
                    bvid/...），None 值的字段跳过，不覆盖已有数据。
                    fail_reason 除外：None 也写入（置 NULL），用于转 DONE 清因。
                    未传 task_id 时默认 'default'（兼容非任务制调用）。
        """
        task_id = fields.pop("task_id", "default")
        # 文档 §6 状态跃迁约束：任何路径转 DONE 时 fail_reason 必须置 NULL
        #（成功与失败原因互斥，禁止共存），历史失败原因一并清空。
        if fields.get("status") == "DONE":
            fields["fail_reason"] = None
        values: dict[str, Any] = {
            k: v for k, v in fields.items() if v is not None or k == "fail_reason"
        }
        values["song_key"] = song_key
        values["task_id"] = task_id
        values["updated_at"] = int(time.time())

        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        update_cols = ", ".join(
            f"{k}=excluded.{k}" for k in values if k not in ("song_key", "task_id")
        )
        if not update_cols:  # 只有主键时退化为 INSERT OR IGNORE
            sql = f"INSERT OR IGNORE INTO songs ({columns}) VALUES ({placeholders})"
        else:
            sql = (
                f"INSERT INTO songs ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(song_key, task_id) DO UPDATE SET {update_cols}"
            )
        self.execute(sql, list(values.values()))

    # ---- 任务制（文档 §4.4/§6）-------------------------------------

    def create_task(self, playlist_id: int | str) -> str:
        """创建 RUNNING 任务，返回唯一 task_id（UUID）。"""
        task_id = uuid.uuid4().hex
        self.execute(
            "INSERT INTO tasks (task_id, playlist_id, status, created_at) VALUES (?, ?, 'RUNNING', ?)",
            (task_id, str(playlist_id), int(time.time())),
        )
        return task_id

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        stats: dict | None = None,
    ) -> None:
        """更新任务状态与统计；置终态（DONE/FAILED）时写 finished_at。"""
        finish = status in ("DONE", "FAILED")
        status_expr = "" if status is None else "status = ?, "
        stats_expr = ", stats = ?" if stats is not None else ""
        sql = (
            f"UPDATE tasks SET {status_expr}finished_at = "
            f"{'?' if finish else 'NULL'}{stats_expr} WHERE task_id = ?"
        )
        params: list[Any] = []
        if status is not None:
            params.append(status)
        if finish:
            params.append(int(time.time()))
        if stats is not None:
            params.append(json.dumps(stats, ensure_ascii=False))
        params.append(task_id)
        self.execute(sql, params)

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))

    def list_tasks(self) -> list[sqlite3.Row]:
        """列出任务及其统计（DONE/MATCHED/MANUAL/FAV_FAILED/总数），新任务在前。"""
        return self.query(
            """
            SELECT t.task_id, t.playlist_id, t.status, t.created_at, t.finished_at,
                   COUNT(s.song_key) AS total_songs,
                   SUM(CASE WHEN s.status = 'DONE' THEN 1 ELSE 0 END) AS done_songs,
                   SUM(CASE WHEN s.status = 'MATCHED' THEN 1 ELSE 0 END) AS matched_songs,
                   SUM(CASE WHEN s.status = 'MANUAL' THEN 1 ELSE 0 END) AS manual_songs,
                   SUM(CASE WHEN s.status = 'FAV_FAILED' THEN 1 ELSE 0 END) AS fav_failed_songs
            FROM tasks t LEFT JOIN songs s ON s.task_id = t.task_id
            GROUP BY t.task_id
            ORDER BY t.created_at DESC
            """
        )

    def delete_task(self, task_id: str) -> None:
        """物理删除任务及其所有 songs 行；search_cache 跨任务共享不删除（文档 §4.4）。"""
        with self._conn:
            self._conn.execute("DELETE FROM songs WHERE task_id = ?", (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    def delete_finished_tasks(self) -> int:
        """批量清理所有终态任务（DONE/FAILED），返回删除数；search_cache 保留。"""
        with self._conn:
            self._conn.execute(
                "DELETE FROM songs WHERE task_id IN "
                "(SELECT task_id FROM tasks WHERE status != 'RUNNING')"
            )
            cur = self._conn.execute(
                "DELETE FROM tasks WHERE status != 'RUNNING'"
            )
            return cur.rowcount

    # ---- 优先级表（文档 §6 whitelist_bv：统一查询入口）-------------

    def upsert_whitelist_bv(self, song_key: str, bvid: str, source: str) -> None:
        """写入/更新 whitelist_bv（优先级统一入口，source: whitelist/manual）。"""
        self.execute(
            "INSERT INTO whitelist_bv (song_key, bvid, source) VALUES (?, ?, ?) "
            "ON CONFLICT(song_key) DO UPDATE SET bvid = excluded.bvid, source = excluded.source",
            (song_key, bvid, source),
        )

    def load_priority_map(self, source: str) -> dict[str, str]:
        """按 source（whitelist/manual）读取 {song_key: bvid}。"""
        rows = self.query("SELECT song_key, bvid FROM whitelist_bv WHERE source = ?", (source,))
        return {r["song_key"]: r["bvid"] for r in rows}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
