"""db.py 单测：建 7 张表可查询验证（验收 1）+ upsert_song 落盘语义 + 任务制方法。"""
from __future__ import annotations

import json

from db import EXPECTED_TABLES, Database


def test_create_all_seven_tables(tmp_path) -> None:
    """建表后 7 张表存在，且 songs/tasks 表结构符合文档 §6（v0.4 任务制）。"""
    db = Database(tmp_path / "test.db")
    try:
        tables = db.table_names()
        assert tables >= EXPECTED_TABLES, f"缺失表: {EXPECTED_TABLES - tables}"

        songs_cols = [r["name"] for r in db.query("PRAGMA table_info(songs)")]
        assert songs_cols == [
            "song_key",
            "task_id",
            "ncm_id",
            "name",
            "artist",
            "album",
            "alia",
            "origin",
            "status",
            "method",
            "bvid",
            "score_detail",
            "fail_reason",
            "updated_at",
        ]

        tasks_cols = [r["name"] for r in db.query("PRAGMA table_info(tasks)")]
        assert "task_id" in tasks_cols and "playlist_id" in tasks_cols
        assert "status" in tasks_cols and "created_at" in tasks_cols
        assert "finished_at" in tasks_cols and "stats" in tasks_cols

        # 各表主键可查询验证（sqlite_master 中 PK 索引存在）
        pk_sql = (
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name IN ({})".format(",".join("?" * len(EXPECTED_TABLES)))
        )
        rows = db.query(pk_sql, list(EXPECTED_TABLES))
        for row in rows:
            assert "PRIMARY KEY" in row["sql"], f"{row['name']} 缺主键"
    finally:
        db.close()


def test_upsert_song_state_transition_persisted(tmp_path) -> None:
    """状态跃迁立即落盘：插入 -> 更新，旧字段保留、新字段写入（默认 task_id）。"""
    db = Database(tmp_path / "test.db")
    try:
        db.upsert_song(
            "歌名|歌手", ncm_id=123, name="歌名", artist="歌手", status="PENDING"
        )
        row = db.query_one("SELECT * FROM songs WHERE song_key = ?", ("歌名|歌手",))
        assert row["status"] == "PENDING"
        assert row["name"] == "歌名"
        assert row["updated_at"] > 0
        assert row["task_id"] == "default"  # 未传 task_id 的默认作用域

        # 跃迁到 DONE：method/bvid 写入，已存在的 name 不被清空
        db.upsert_song(
            "歌名|歌手", status="DONE", method="SCORED", bvid="BV1xxxx", name=None
        )
        row = db.query_one("SELECT * FROM songs WHERE song_key = ?", ("歌名|歌手",))
        assert row["status"] == "DONE"
        assert row["method"] == "SCORED"
        assert row["bvid"] == "BV1xxxx"
        assert row["name"] == "歌名"  # None 不覆盖
        assert row["ncm_id"] == 123

        # 全新歌曲插入
        db.upsert_song("另一首|另一个歌手", status="MANUAL", fail_reason="未匹配")
        rows = db.query("SELECT song_key FROM songs ORDER BY song_key")
        assert len(rows) == 2
    finally:
        db.close()


def test_done_paths_clear_fail_reason(tmp_path) -> None:
    """文档 §6 状态跃迁约束：四条 DONE 路径（MANUAL/UPLOADER_WL/WHITELIST_BV/
    SCORED）写库后 fail_reason 必须为 NULL，历史失败原因一并清空。"""
    db = Database(tmp_path / "test.db")
    try:
        for i, method in enumerate(["MANUAL", "UPLOADER_WL", "WHITELIST_BV", "SCORED"]):
            key = f"歌{i}|艺{i}"
            # 预置历史失败原因（如曾被降级链置 MANUAL）
            db.upsert_song(key, status="MANUAL", method="MANUAL",
                           fail_reason="匹配失败：降级链全部未命中")
            assert db.query_one(
                "SELECT fail_reason FROM songs WHERE song_key = ?", (key,)
            )["fail_reason"] is not None

            # 转 DONE：无论调用方是否传 fail_reason，落库后必须为 NULL
            db.upsert_song(key, status="DONE", method=method, bvid=f"BV{i}")
            row = db.query_one("SELECT status, method, fail_reason FROM songs WHERE song_key = ?", (key,))
            assert row["status"] == "DONE"
            assert row["method"] == method
            assert row["fail_reason"] is None

        # 反向不误伤：非 DONE（MANUAL）仍保留失败原因
        db.upsert_song("未匹配|歌手", status="MANUAL", fail_reason="闸门未达标")
        assert db.query_one(
            "SELECT fail_reason FROM songs WHERE song_key = '未匹配|歌手'"
        )["fail_reason"] == "闸门未达标"
    finally:
        db.close()


def test_done_with_explicit_none_fail_reason(tmp_path) -> None:
    """显式传 fail_reason=None 的 DONE 写入也可落库清因（不再被 None 过滤丢弃）。"""
    db = Database(tmp_path / "test.db")
    try:
        db.upsert_song("歌A|艺A", status="MANUAL", fail_reason="SEARCH_FAILED")
        db.upsert_song("歌A|艺A", status="DONE", method="MANUAL", bvid="BV1",
                       fail_reason=None)
        row = db.query_one("SELECT fail_reason FROM songs WHERE song_key = '歌A|艺A'")
        assert row["fail_reason"] is None
    finally:
        db.close()


def test_execute_and_query_wrapper(tmp_path) -> None:
    """execute/query 封装：写后立即可查，行按列名访问。"""
    db = Database(tmp_path / "test.db")
    try:
        db.execute(
            "INSERT INTO kv_meta (key, value, fetched_at) VALUES (?, ?, ?)",
            ("wbi_keys", '{"img":"a","sub":"b"}', 12345),
        )
        row = db.query_one("SELECT * FROM kv_meta WHERE key = ?", ("wbi_keys",))
        assert row["value"] == '{"img":"a","sub":"b"}'
        assert row["fetched_at"] == 12345
    finally:
        db.close()


# ---- v0.4 任务制 -------------------------------------------------------


def test_create_task_and_upsert_song_with_task_id(tmp_path) -> None:
    """创建任务返回唯一 task_id；同 song_key 不同 task 互不覆盖（联合主键）。"""
    db = Database(tmp_path / "test.db")
    try:
        t1 = db.create_task(12345)
        t2 = db.create_task(12345)
        assert t1 != t2
        assert db.get_task(t1)["status"] == "RUNNING"

        db.upsert_song("歌|艺", task_id=t1, status="DONE", method="SCORED", bvid="BV1")
        db.upsert_song("歌|艺", task_id=t2, status="MANUAL", method="MANUAL")

        rows = db.query(
            "SELECT task_id, status, bvid FROM songs WHERE song_key = '歌|艺'"
        )
        by_task = {r["task_id"]: (r["status"], r["bvid"]) for r in rows}
        assert len(rows) == 2  # 两任务各一条
        assert by_task[t1] == ("DONE", "BV1")
        assert by_task[t2] == ("MANUAL", None)
    finally:
        db.close()


def test_update_task_status_and_stats(tmp_path) -> None:
    """update_task：终态写 finished_at，stats 落 JSON。"""
    db = Database(tmp_path / "test.db")
    try:
        t = db.create_task(1)
        assert db.get_task(t)["finished_at"] is None

        db.update_task(t, status="DONE", stats={"total": 5, "done": 4, "manual": 1})
        row = db.get_task(t)
        assert row["status"] == "DONE"
        assert row["finished_at"] is not None
        assert json.loads(row["stats"]) == {"total": 5, "done": 4, "manual": 1}
    finally:
        db.close()


def test_list_tasks_shows_stats(tmp_path) -> None:
    """list_tasks：任务与其 DONE/MANUAL/总数统计一次性返回，新任务在前。"""
    db = Database(tmp_path / "test.db")
    try:
        t1 = db.create_task("p1")
        t2 = db.create_task("p2")
        # t1 有 2 首（1 DONE + 1 MANUAL）
        db.upsert_song("a1|艺", task_id=t1, status="DONE")
        db.upsert_song("a2|艺", task_id=t1, status="MANUAL")
        # t2 空
        rows = db.list_tasks()
        by_id = {r["task_id"]: r for r in rows}
        assert by_id[t1]["total_songs"] == 2
        assert by_id[t1]["done_songs"] == 1
        assert by_id[t1]["manual_songs"] == 1
        assert by_id[t2]["total_songs"] == 0
        # 两个任务都列出；新任务在前（created_at 同秒时顺序不稳定，只校验都在）
        assert {r["task_id"] for r in rows} == {t1, t2}
        assert rows[0]["created_at"] >= rows[1]["created_at"]
    finally:
        db.close()


def test_delete_task_keeps_search_cache(tmp_path) -> None:
    """delete_task：songs 清空、任务记录删除、search_cache 保留（跨任务共享）。"""
    import time as _time

    db = Database(tmp_path / "test.db")
    try:
        t = db.create_task(1)
        db.upsert_song("歌|艺", task_id=t, status="DONE", method="SCORED", bvid="BV1")
        db.execute(
            "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
            ("歌 艺", '[{"bvid":"BV1"}]', int(_time.time())),
        )

        db.delete_task(t)

        assert db.get_task(t) is None
        assert db.query_one("SELECT COUNT(*) AS n FROM songs")["n"] == 0
        # search_cache 保留
        assert db.query_one("SELECT * FROM search_cache WHERE keyword = '歌 艺'") is not None
    finally:
        db.close()


def test_delete_finished_tasks_keeps_running(tmp_path) -> None:
    """delete_finished_tasks：只清终态任务，RUNNING 与 search_cache 保留。"""
    import time as _time

    db = Database(tmp_path / "test.db")
    try:
        running = db.create_task(1)
        done = db.create_task(2)
        db.update_task(done, status="DONE")
        db.upsert_song("歌|艺", task_id=running, status="DONE", method="SCORED")
        db.upsert_song("歌2|艺2", task_id=done, status="DONE", method="SCORED")
        db.execute(
            "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
            ("kw", '[]', int(_time.time())),
        )

        deleted = db.delete_finished_tasks()
        assert deleted == 1  # 仅 done 任务
        assert db.get_task(running) is not None  # RUNNING 保留
        assert db.query_one("SELECT COUNT(*) AS n FROM songs")["n"] == 1  # 仅 running 的歌
        assert db.query_one("SELECT * FROM search_cache WHERE keyword = 'kw'") is not None
    finally:
        db.close()


def test_migrate_legacy_songs_table(tmp_path) -> None:
    """v0.3 → v0.4 迁移：无 task_id 的旧 songs 表被重建，旧数据归入 legacy 任务。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE songs (
          song_key TEXT PRIMARY KEY, ncm_id INTEGER, name TEXT, artist TEXT,
          album TEXT, alia TEXT, origin TEXT, status TEXT, method TEXT,
          bvid TEXT, score_detail TEXT, fail_reason TEXT, updated_at INTEGER
        );
        INSERT INTO songs (song_key, name, status, bvid)
        VALUES ('歌|艺', '歌', 'DONE', 'BV1legacy');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        # 新结构带 task_id 列，旧数据保留
        col_names = [r["name"] for r in db.query("PRAGMA table_info(songs)")]
        assert "task_id" in col_names
        row = db.query_one("SELECT * FROM songs WHERE song_key = '歌|艺'")
        assert row["task_id"] == "legacy"
        assert row["bvid"] == "BV1legacy"
    finally:
        db.close()


def test_migrate_done_to_matched_v044(tmp_path) -> None:
    """F2-1（§4.4 迁移策略）：老库（v0.4.2 语义，全 DONE=已匹配）打开后自动转
    MATCHED（已匹配待收藏），交阶段三 deal 补收藏——deal 幂等，宁可重复收藏
    也不漏收藏；kv_meta 打标记防重复迁移，迁移后新写入的 DONE（fav_one 收藏
    成功）不被二次翻转。"""
    import sqlite3

    path = tmp_path / "legacy044.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE songs (
          song_key TEXT, task_id TEXT, ncm_id INTEGER, name TEXT, artist TEXT,
          album TEXT, alia TEXT, origin TEXT, status TEXT, method TEXT,
          bvid TEXT, score_detail TEXT, fail_reason TEXT, updated_at INTEGER,
          PRIMARY KEY (song_key, task_id)
        );
        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY, playlist_id TEXT, status TEXT,
          created_at INTEGER, finished_at INTEGER, stats TEXT
        );
        INSERT INTO tasks (task_id, playlist_id, status, created_at)
        VALUES ('t-legacy', '123', 'DONE', 1);
        INSERT INTO songs (song_key, task_id, name, artist, status, method, bvid, updated_at)
        VALUES
          ('歌1|艺1', 't-legacy', '歌1', '艺1', 'DONE', 'SCORED', 'BV1old', 1),
          ('歌2|艺2', 't-legacy', '歌2', '艺2', 'DONE', 'MANUAL', 'BV2old', 1);
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        # 打开即迁移：全 DONE → MATCHED，且写入一次性标记
        rows = db.query(
            "SELECT song_key, status FROM songs WHERE task_id = 't-legacy' ORDER BY song_key"
        )
        assert [(r["song_key"], r["status"]) for r in rows] == [
            ("歌1|艺1", "MATCHED"),
            ("歌2|艺2", "MATCHED"),
        ]
        assert db.query_one(
            "SELECT value FROM kv_meta WHERE key = 'migrated_done_to_matched_v044'"
        ) is not None

        # 迁移后 fav_one 收藏成功写 DONE（新语义的终态）
        db.upsert_song("歌1|艺1", task_id="t-legacy", status="DONE",
                       method="SCORED", bvid="BV1old")
    finally:
        db.close()

    # 再次打开：标记已存在，不二次迁移——DONE 保持 DONE（否则收藏永远无法收敛）
    db = Database(path)
    try:
        row = db.query_one(
            "SELECT status FROM songs WHERE song_key = '歌1|艺1' AND task_id = 't-legacy'"
        )
        assert row["status"] == "DONE"
    finally:
        db.close()
