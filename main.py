"""CLI 入口（里程碑 M4：run --dry-run 骨架，对应文档 §10.1）。

流程：阶段一（网易云抓取）→ 阶段二（状态机匹配，并发按 config）→
      生成 output/preview_report.html + report.csv。
--dry-run 不调用任何收藏夹创建/收藏接口（文档 §10.1）。

中断后可重跑：songs 表中已 DONE 的歌直接跳过，不再发搜索请求（§4.4 断点续跑）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

import httpx

from auth import run_auth
from bili_search import BiliSearchClient
from circuit_breaker import CircuitBreaker
from config import Config, load_config
from db import Database
from logging_setup import setup_logging
from matcher import Matcher
from ncm import NcmClient
from report import write_reports, write_review_html

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = Path("output")


def _session_headers(config: Config) -> dict[str, str]:
    """文档 §7 身份层：启动时从 config.http.user_agents 随机选一个 UA 并全程固定。

    返回配套 header 集合（User-Agent + Referer），贯穿搜索/阶段二/收藏同一 session。
    """
    return {
        "User-Agent": random.choice(config.http.user_agents),
        "Referer": config.http.referer,
    }


def _load_json(path: str | Path, default: Any) -> Any:
    """读取 JSON 配置；文件缺失、为空或损坏时回退到 default。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


async def run_dry_run(
    playlist_id: int,
    config: Config,
    db: Database,
    ncm: NcmClient,
    bili: BiliSearchClient,
    http: httpx.AsyncClient,
    *,
    output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
    whitelist: dict | None = None,
    uploaders: dict | None = None,
    blacklist_words: list[str] | None = None,
    manual: dict | None = None,
    refresh: bool = False,
    task_id: str | None = None,
) -> dict:
    """dry-run 主流程（可编程入口，测试直接注入 clients）。

    Args:
        playlist_id: 网易云歌单 ID。
        output_dir: 报告输出目录（默认 output/）。
        whitelist/uploaders/blacklist_words/manual: 为 None 时从项目根 json 文件加载。
        refresh: 文档 §4.4 --refresh 语义：丢弃当前任务匹配结果（重置 songs 状态），
                 保留 search_cache 与歌单数据；True 时全部歌曲重新匹配。
        task_id: 指定任务（resume 复用现有任务）；None 时创建新 RUNNING 任务（文档 §4.4）。
    """
    if whitelist is None:
        whitelist = _load_json("whitelist.json", {})
    if uploaders is None:
        uploaders = _load_json("uploaders.json", {})
    if blacklist_words is None:
        blacklist_words = _load_json("blacklist_words.json", [])
    if manual is None:
        manual = _load_json("manual.json", {})

    # 文档 §4.4 任务制：无 task_id 时创建新任务；否则沿用传入任务（resume）
    if task_id is None:
        task_id = db.create_task(playlist_id)
    else:
        db.update_task(task_id, status="RUNNING")  # resume：任务状态置 RUNNING

    if refresh:
        # 文档 §4.4：丢弃当前任务匹配结果，保留 search_cache 与歌单数据
        db.execute(
            "UPDATE songs SET status='PENDING', method=NULL, bvid=NULL, "
            "score_detail=NULL, fail_reason=NULL WHERE task_id = ?",
            (task_id,),
        )

    # 文档 §4.1 优先级重查：已 DONE 的歌若 manual.json 新增了对应 BV 且与现有不同，
    # 升级为人工结果（method=MANUAL）；不发任何网络请求。
    if manual:
        rows = db.query(
            "SELECT song_key, bvid FROM songs WHERE status = 'DONE' AND task_id = ?",
            (task_id,),
        )
        for row in rows:
            key = row["song_key"]
            manual_bv = manual.get(key)
            if manual_bv and manual_bv != row["bvid"]:
                db.upsert_song(
                    key, task_id=task_id, status="DONE", method="MANUAL", bvid=manual_bv
                )

    # 阶段一：网易云抓取（文档 §5.1）
    track_ids = await ncm.fetch_playlist_track_ids(playlist_id)
    songs = await ncm.fetch_song_details(track_ids)

    matcher = Matcher(
        config,
        db,
        bili,
        ncm,
        http,
        task_id=task_id,
        whitelist=whitelist,
        uploaders=uploaders,
        blacklist_words=blacklist_words,
        manual=manual,
    )

    # 阶段二：跳过已 DONE / FAV_FAILED 的歌（断点续跑 §4.4，按 task_id 隔离）。
    # FAV_FAILED 已完成匹配（bvid 已存），resume 只重跑阶段三收藏，不回 matcher。
    pending: list[dict] = []
    for song in songs:
        key = Matcher.song_key(song)
        row = db.query_one(
            "SELECT status FROM songs WHERE song_key = ? AND task_id = ?", (key, task_id)
        )
        if row is not None and row["status"] in ("DONE", "FAV_FAILED"):
            continue
        pending.append(song)

    sem = asyncio.Semaphore(config.rate_limit.search.concurrency)  # 文档 §7 搜索并发 4
    search_rl = config.rate_limit.search

    async def worker(song: dict) -> dict:
        async with sem:
            # 文档 §7 预防层：间隔 jitter
            await asyncio.sleep(random.uniform(*search_rl.jitter_ms) / 1000)
            return await matcher.match_song(song)

    # 任一歌中断（网络/风控异常）时，取消并等待其余 task 收尾，
    # 避免后台残留 task 与下一次 run 并发导致落盘时序混乱（断点续跑依赖确定性）
    tasks = [asyncio.create_task(worker(song)) for song in pending]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # 文档 §4.4：中断保留 RUNNING 状态（任务可被 task resume 恢复），
        # 但进度统计已按已落盘 songs 为准，无需额外写库
        raise

    done = sum(1 for r in results if r["status"] == "DONE")
    manual = sum(1 for r in results if r["status"] == "MANUAL")
    db.update_task(
        task_id,
        status="DONE",
        stats={"total": len(songs), "done": done, "manual": manual},
    )

    # 生成报告（dry-run 不建收藏夹、不收藏，见文档 §10.1）
    # 文档 §3 阶段三"报告（绑定 task_id）"：仅报告当前任务的歌曲
    csv_path, html_path = write_reports(db, output_dir, task_id=task_id)

    methods: dict[str, int] = {}
    for result in results:
        methods[result["method"]] = methods.get(result["method"], 0) + 1
    return {
        "task_id": task_id,
        "total": len(songs),
        "processed": len(results),
        "skipped_done": len(songs) - len(pending),
        "methods": methods,
        "reports": [str(csv_path), str(html_path)],
        "playlist_name": await ncm.fetch_playlist_name(playlist_id),
    }


async def run_formal(
    playlist_id: int,
    config: Config,
    db: Database,
    ncm: NcmClient,
    bili: BiliSearchClient,
    http: httpx.AsyncClient,
    *,
    cookie_str: str,
    output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
    whitelist: dict | None = None,
    uploaders: dict | None = None,
    blacklist_words: list[str] | None = None,
    manual: dict | None = None,
    refresh: bool = False,
    task_id: str | None = None,
    session_headers: dict[str, str] | None = None,
) -> dict:
    """正式运行：dry-run 全流程（阶段一/二）+ 阶段三批量收藏（文档 §5.3/§9.3）。

    阶段三要点：
    - 按 900/夹拆分、复用已有同名夹 media_id（§5.3）；
    - 幂等三分支：已收藏→DONE、视频失效→MANUAL、其他失败→FAV_FAILED（§9.3）；
    - -101/-111 立即中止并提示重新 auth（§9.2）；建夹失败 -400 停止保留断点；
    - session_headers: 阶段一启动时选定的 UA/Referer（§7 身份层），收藏客户端沿用。
    """
    counts = await run_dry_run(
        playlist_id,
        config,
        db,
        ncm,
        bili,
        http,
        output_dir=output_dir,
        whitelist=whitelist,
        uploaders=uploaders,
        blacklist_words=blacklist_words,
        manual=manual,
        refresh=refresh,
        task_id=task_id,
    )
    task_id = counts["task_id"]

    # 阶段三：从 db 读取匹配成功（DONE 且有 bvid）的歌曲进入收藏（按 task 作用域）
    rows = db.query(
        "SELECT * FROM songs WHERE status = 'DONE' AND bvid IS NOT NULL AND task_id = ?",
        (task_id,),
    )
    fav_songs_list = [dict(r) for r in rows]

    from fav import AuthExpiredError, BiliFavClient, fav_songs

    fav_client = BiliFavClient(
        http,
        db,
        config,
        cookie_str,
        user_agent=(session_headers or {}).get("User-Agent"),  # §7 身份层同 session UA
        breaker=CircuitBreaker(config.risk_control.circuit_breaker),  # 文档 §7 响应层 b
    )
    try:
        summary = await fav_songs(
            fav_client, db, config, fav_songs_list, counts["playlist_name"]
        )
    except AuthExpiredError:
        raise  # 文档 §9.2：凭证失效有明确语义（提示重新 auth），不被兜底吞掉
    except Exception as exc:  # noqa: BLE001 - 兜底：不允许裸 traceback 给用户
        # 文档 §5.3：建夹失败/未预期异常 → 记录 ERROR、保留断点、非零码退出
        logger.error("阶段三收藏失败（断点已保留，修复后重跑可直接续跑）: %s", exc, exc_info=True)
        raise SystemExit(1) from exc
    counts["fav"] = summary
    return counts


# ---- CLI ---------------------------------------------------------------


def _report_command(args: argparse.Namespace) -> None:
    """report 命令：生成 review.html（MANUAL 歌曲回灌页），可选拉起本地保存服务。"""
    db = Database("cache.db")
    try:
        if args.task_id:
            rows = db.query(
                "SELECT * FROM songs WHERE task_id = ? ORDER BY song_key", (args.task_id,)
            )
        else:
            rows = db.query("SELECT * FROM songs ORDER BY song_key")
        rows = [dict(r) for r in rows]

        save_url: str | None = None
        if args.serve:
            from review_server import serve_in_background

            server, port = serve_in_background("manual.json", db=db)
            save_url = f"http://127.0.0.1:{port}/save"
            print(f"本地保存服务已启动：{save_url}（Ctrl+C 停止）")

        html_path = write_review_html(rows, _DEFAULT_OUTPUT_DIR / "review.html", save_url=save_url)
        print(f"review.html 已生成: {html_path}")

        if args.serve:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.shutdown()
                server.server_close()
    finally:
        db.close()


def _auth_command(args: argparse.Namespace) -> None:
    """auth 命令：playwright 浏览器授权，或 --paste 手动粘贴回退（文档 §11.3）。"""
    db = Database("credentials.db")
    try:
        run_auth(db, paste=args.paste)
    except Exception as exc:  # noqa: BLE001 - 授权失败以可读信息退出
        print(f"auth 失败：{exc}")
        raise SystemExit(1) from exc
    finally:
        db.close()


def _run_command(args: argparse.Namespace) -> None:
    config = load_config()
    # 文档 §7 身份层：启动时随机选一个 UA，全程固定贯穿搜索/阶段二/收藏
    session_headers = _session_headers(config)
    db = Database("cache.db")
    ncm = NcmClient(httpx.AsyncClient())
    bili = BiliSearchClient(
        httpx.AsyncClient(),
        db=db,
        breaker=CircuitBreaker(config.risk_control.circuit_breaker),  # 文档 §7 响应层 b
        headers=session_headers,
    )
    http = httpx.AsyncClient(headers=session_headers)

    async def _main() -> None:
        try:
            await _body()
        except SystemExit:
            raise  # 收藏阶段已有明确错误语义（提示重新 auth 等），原样退出
        except Exception as exc:  # noqa: BLE001 - 顶层兜底（文档 §13：不允许裸 traceback）
            # 文档 §4.4：断点已按任务落盘，修复后 task resume 可直接续跑
            logger.error("运行失败（任务断点已保留，可 task resume 续跑）: %s", exc, exc_info=True)
            raise SystemExit(1) from exc

    async def _body() -> None:
        if args.dry_run:
            counts = await run_dry_run(
                args.playlist_id,
                config,
                db,
                ncm,
                bili,
                http,
                output_dir=_DEFAULT_OUTPUT_DIR,
                refresh=args.refresh,
            )
            print(f"任务 {counts['task_id']} 完成")
            print(
                f"共 {counts['total']} 首，处理 {counts['processed']} 首，"
                f"跳过 {counts['skipped_done']} 首（已 DONE）"
            )
            print("method 分布:", counts["methods"])
            for path in counts["reports"]:
                print(f"报告已生成: {path}")
            return

        # 正式运行：阶段三收藏需要 B 站凭证（文档 §11.3 auth）
        cookie_str = _load_cookie()
        counts = await run_formal(
            args.playlist_id,
            config,
            db,
            ncm,
            bili,
            http,
            cookie_str=cookie_str,
            output_dir=_DEFAULT_OUTPUT_DIR,
            refresh=args.refresh,
            session_headers=session_headers,
        )
        print(f"任务 {counts['task_id']} 完成")
        print(
            f"共 {counts['total']} 首，处理 {counts['processed']} 首，"
            f"跳过 {counts['skipped_done']} 首（已 DONE）"
        )
        print("method 分布:", counts["methods"])
        print("收藏结果:", counts["fav"])
        for path in counts["reports"]:
            print(f"报告已生成: {path}")

    asyncio.run(_main())


def _print_task_summary(row, index: int | None = None) -> None:
    """单行任务摘要（list 与 delete/resume 确认共用）。"""
    import datetime as _dt

    created = _dt.datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M")
    stats = (row["total_songs"], row["done_songs"] or 0, row["manual_songs"] or 0)
    fav_failed = row["fav_failed_songs"] or 0
    prefix = f"[{index}] " if index is not None else ""
    print(
        f"{prefix}task={row['task_id']} 歌单={row['playlist_id']} 状态={row['status']} "
        f"DONE{stats[1]}/MANUAL{stats[2]}/FAV_FAILED{fav_failed}/总数{stats[0]} 创建于 {created}"
    )


def _confirm(prompt: str, yes: bool) -> bool:
    """交互确认：y/N，--yes 直接通过；非交互终端无输入时默认拒绝。"""
    if yes:
        return True
    ans = input(f"{prompt} [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _task_list_command(args: argparse.Namespace) -> None:
    db = Database("cache.db")
    try:
        rows = db.list_tasks()
        if not rows:
            print("暂无任务")
            return
        for i, row in enumerate(rows, 1):
            _print_task_summary(row, i)
    finally:
        db.close()


def _task_delete_command(args: argparse.Namespace) -> None:
    db = Database("cache.db")
    try:
        if args.all_finished:
            row = db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE status != 'RUNNING'"
            )
            n = row["n"]
            if n == 0:
                print("没有已完成的旧任务")
                return
            if not _confirm(f"删除 {n} 个已完成任务（songs 清空，search_cache 保留）?", args.yes):
                print("已取消")
                return
            deleted = db.delete_finished_tasks()
            print(f"已删除 {deleted} 个任务，search_cache 保留")
            return

        summary = db.query_one(
            """
            SELECT t.task_id, t.playlist_id, t.status, t.created_at,
                   COUNT(s.song_key) AS total_songs,
                   SUM(CASE WHEN s.status = 'DONE' THEN 1 ELSE 0 END) AS done_songs,
                   SUM(CASE WHEN s.status = 'MANUAL' THEN 1 ELSE 0 END) AS manual_songs,
                   SUM(CASE WHEN s.status = 'FAV_FAILED' THEN 1 ELSE 0 END) AS fav_failed_songs
            FROM tasks t LEFT JOIN songs s ON s.task_id = t.task_id
            WHERE t.task_id = ? GROUP BY t.task_id
            """,
            (args.task_id,),
        )
        if summary is None:
            print(f"任务不存在: {args.task_id}")
            raise SystemExit(1)
        _print_task_summary(summary)
        if not _confirm(
            f"删除任务 {args.task_id}（songs 清空，search_cache 保留，不可恢复）?", args.yes
        ):
            print("已取消")
            return
        db.delete_task(args.task_id)
        print(f"已删除任务 {args.task_id}，search_cache 保留")
    finally:
        db.close()


def _task_resume_command(args: argparse.Namespace) -> None:
    db = Database("cache.db")
    task = db.get_task(args.task_id)
    if task is None:
        print(f"任务不存在: {args.task_id}")
        db.close()
        raise SystemExit(1)

    summary = db.query_one(
        """
        SELECT t.task_id, t.playlist_id, t.status, t.created_at,
               COUNT(s.song_key) AS total_songs,
               SUM(CASE WHEN s.status = 'DONE' THEN 1 ELSE 0 END) AS done_songs,
               SUM(CASE WHEN s.status = 'MANUAL' THEN 1 ELSE 0 END) AS manual_songs,
               SUM(CASE WHEN s.status = 'FAV_FAILED' THEN 1 ELSE 0 END) AS fav_failed_songs
        FROM tasks t LEFT JOIN songs s ON s.task_id = t.task_id
        WHERE t.task_id = ? GROUP BY t.task_id
        """,
        (args.task_id,),
    )
    _print_task_summary(summary)
    if not _confirm(
        f"resume 任务 {args.task_id}（已 DONE 不重复匹配，仅处理未完成歌曲）?", args.yes
    ):
        db.close()
        print("已取消")
        return

    config = load_config()
    # 文档 §7 身份层：resume 与 run 同款——启动时随机选 UA 全程固定
    session_headers = _session_headers(config)
    ncm = NcmClient(httpx.AsyncClient())
    # 文档 §7 响应层 b：resume 与 run 同款注入全局熔断器（-412/-702 触发熔断）
    bili = BiliSearchClient(
        httpx.AsyncClient(),
        db=db,
        breaker=CircuitBreaker(config.risk_control.circuit_breaker),
        headers=session_headers,
    )
    http = httpx.AsyncClient(headers=session_headers)

    async def _main() -> None:
        try:
            await _body()
        except SystemExit:
            raise  # 收藏重试已有明确错误语义，原样退出
        except Exception as exc:  # noqa: BLE001 - 顶层兜底（文档 §13：不裸奔 traceback）
            logger.error("任务恢复失败（断点已保留，修复后可再次 resume）: %s", exc, exc_info=True)
            raise SystemExit(1) from exc

    async def _body() -> None:
        counts = await run_dry_run(
            int(task["playlist_id"]),
            config,
            db,
            ncm,
            bili,
            http,
            output_dir=_DEFAULT_OUTPUT_DIR,
            task_id=args.task_id,
        )
        print(f"任务 {counts['task_id']} 恢复完成")
        print(
            f"共 {counts['total']} 首，处理 {counts['processed']} 首，"
            f"跳过 {counts['skipped_done']} 首（已 DONE）"
        )
        print("method 分布:", counts["methods"])
        for path in counts["reports"]:
            print(f"报告已生成: {path}")

        # 文档 §4.4：FAV_FAILED 歌已匹配完成（bvid 已存），resume 只重跑
        # 阶段三收藏，不再回 matcher/搜索；收藏夹按 §5.3 名称复用，不为重试重复建夹。
        retry_rows = db.query(
            "SELECT * FROM songs WHERE status = 'FAV_FAILED' AND bvid IS NOT NULL AND task_id = ?",
            (args.task_id,),
        )
        if not retry_rows:
            return

        from fav import AuthExpiredError, BiliFavClient, fav_songs

        cookie_str = _load_cookie()
        fav_client = BiliFavClient(
            http,
            db,
            config,
            cookie_str,
            user_agent=session_headers.get("User-Agent"),  # §7 身份层同 session UA
            breaker=CircuitBreaker(config.risk_control.circuit_breaker),  # §7 响应层 b
        )
        try:
            summary = await fav_songs(
                fav_client, db, config, [dict(r) for r in retry_rows], counts["playlist_name"]
            )
        except AuthExpiredError:
            raise  # 文档 §9.2：凭证失效提示重新 auth，不被兜底吞掉
        except Exception as exc:  # noqa: BLE001 - 兜底保留断点，允许再次 resume
            logger.error(
                "阶段三收藏重试失败（断点已保留，修复后可再次 resume）: %s", exc, exc_info=True
            )
            raise SystemExit(1) from exc
        print(f"收藏重试（FAV_FAILED {len(retry_rows)} 首）结果: {summary}")

    try:
        asyncio.run(_main())
    finally:
        db.close()


def _load_cookie() -> str:
    """从 credentials.db 读取并解密 B 站 cookie；无凭证时提示重新 auth。"""
    from auth import load_credentials, load_or_create_key

    cred_db = Database("credentials.db")
    try:
        key = load_or_create_key()
        cookie = load_credentials(cred_db, key)
    finally:
        cred_db.close()
    if not cookie:
        print("未找到 B 站凭证，请先执行 `python main.py auth` 授权")
        raise SystemExit(1)
    return cookie


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ncm2bili", description="网易云歌单 → B站收藏夹 转换工具（文档 v0.4 任务制）"
    )
    # 文档 §12：DEBUG 级日志仅 --debug 时开启（cookie 脱敏照常生效）
    parser.add_argument(
        "--debug", action="store_true", help="开启 DEBUG 级日志（控制台与文件；cookie 仍脱敏）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    auth_p = sub.add_parser("auth", help="B 站授权：登录后加密保存凭证（重复执行视为刷新）")
    auth_p.add_argument(
        "--paste", action="store_true", help="无桌面环境回退：手动粘贴 Cookie 字符串"
    )
    run_p = sub.add_parser("run", help="运行匹配流程（每次自动创建新任务）")
    run_p.add_argument("playlist_id", type=int, help="网易云歌单 ID")
    run_p.add_argument(
        "--dry-run", action="store_true", help="只匹配不收藏，生成 preview_report.html（推荐首次使用）"
    )
    run_p.add_argument(
        "--refresh", action="store_true", help="丢弃匹配结果重新匹配（保留 search_cache，文档 §4.4）"
    )
    task_p = sub.add_parser("task", help="任务管理（list / resume / delete）")
    task_sub = task_p.add_subparsers(dest="task_action", required=True)
    task_p_list = task_sub.add_parser("list", help="列出所有任务及状态")
    task_p_resume = task_sub.add_parser("resume", help="恢复指定任务的断点，从失败处继续")
    task_p_resume.add_argument("task_id", help="任务 ID")
    task_p_delete = task_sub.add_parser("delete", help="删除任务所有状态与中间结果（search_cache 保留）")
    task_p_delete.add_argument("task_id", nargs="?", default=None, help="任务 ID")
    task_p_delete.add_argument(
        "--all-finished", action="store_true", help="批量清理所有已完成任务"
    )
    for tp in (task_p_list, task_p_resume, task_p_delete):
        tp.add_argument(
            "--yes", action="store_true", help="跳过确认直接执行（仅 resume/delete 生效）"
        )
    report_p = sub.add_parser("report", help="生成人工回灌页 review.html（MANUAL 歌曲）")
    report_p.add_argument(
        "--serve", action="store_true", help="同时启动本地保存服务（127.0.0.1 随机端口）"
    )
    report_p.add_argument("--task-id", default=None, help="仅报告指定任务的歌曲（默认全部）")
    args = parser.parse_args(argv)

    # 文档 §12 日志初始化：挂到 root logger（模块 logger 继承），DEBUG 仅 --debug 开启
    setup_logging(
        level=logging.DEBUG if args.debug else logging.INFO,
        logger_name="",
    )

    if args.command == "auth":
        _auth_command(args)
    elif args.command == "run":
        _run_command(args)
    elif args.command == "task":
        if args.task_action == "list":
            _task_list_command(args)
        elif args.task_action == "resume":
            _task_resume_command(args)
        elif args.task_action == "delete":
            _task_delete_command(args)
        else:
            task_p.error(f"未支持的 task 操作: {args.task_action}")
    elif args.command == "report":
        _report_command(args)
    else:
        parser.error(f"未支持的命令: {args.command}")


if __name__ == "__main__":
    main()
