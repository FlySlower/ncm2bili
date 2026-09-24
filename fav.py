"""收藏夹创建与批量收藏（里程碑 M5，对应文档 §5.3 / §9.3）。

- 按 config.fav.per_folder_limit（900）拆分，命名 "<歌单名> (1)/(2)..."，
  重跑时按名称查询复用已有收藏夹的 media_id（文档 §5.3）；
- 幂等三分支（文档 §9.3）：
  * 已收藏 → 视为成功 DONE；
  * 视频不存在（-404 等）→ 回 MANUAL 并注明；
  * 其他失败 → 置 FAV_FAILED 并记 fail_reason（不回 DONE；resume 时
    单独重跑阶段三，文档 §4.4）。
- 收藏并发与间隔由 config.fav 决定（默认新账号保守档，文档 §7，不在此硬编码）；
  412 走文档 §7 响应层单请求指数退避；
- -101/-111 立即中止并提示重新 auth（文档 §9.2），不做退避重试；
- 建夹失败（99 上限 -400）→ 停止收藏阶段，保留断点（文档 §5.3）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any

import httpx

from backoff import backoff_delays, sleep_before_retry
from bili_search import _DEFAULT_BROWSER_UA
from circuit_breaker import CircuitBreaker
from config import Config
from db import Database

logger = logging.getLogger(__name__)

# 文档 §5.3/§9.3 接口
_FOLDER_LIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
_FOLDER_ADD_URL = "https://api.bilibili.com/x/v3/fav/folder/add"
# 收藏接口：实测 2026-09-22 /x/v3/fav/resource/add 已废弃（404），
# 现行路径为 /x/v3/fav/resource/deal（HTTP 200 + JSON code）
_RESOURCE_ADD_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"
# 视频详情（bvid→aid 转换）
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
# nav 接口（up_mid 兜底：DedeUserID 缺失时取 data.mid）
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# ---- 文档 §9.3 幂等三分支判定码 --------------------------------------
# 确认日期：2026-09-21（以实测为准，如后续实测到专有"已在夹中" code 在此补充）。
# 当前按公开实现与实测经验：重复添加同一视频返回 code 0（幂等成功），
# 与首次收藏成功同码，均视为 DONE（F4-5：code 0 即唯一成功码，无需额外集合）。
# 视频不存在/被删（文档 §9.3 示例）
VIDEO_NOT_FOUND_CODES: frozenset[int] = frozenset({-404, 62002})
# 认证失效（文档 §9.2）：立即中止，不做退避重试
AUTH_EXPIRED_CODES: frozenset[int] = frozenset({-101, -111})
# 目标账号写限流（实测 2026-09-22：HTTP 200 + code -702 "请求频率过高"）：
# 与 -412 同级走风控层，但退避基数 2 倍（4s→8s→16s），计入全局熔断
RATE_LIMIT_CODE: int = -702
# 自适应降速：阶段三内连续 -702 达此数 → 剩余请求 interval 翻倍（上限倍数）
RATE_LIMIT_CONSECUTIVE_THRESHOLD: int = 2
RATE_LIMIT_MAX_MULTIPLIER: float = 4.0


class FavError(Exception):
    """收藏接口请求失败。"""


class _RiskError(Exception):
    """内部标记：HTTP 412 风控响应（由 _request 消化）。"""


class AuthExpiredError(FavError):
    """凭证失效（-101/-111）：立即中止当前阶段，提示重新 auth。"""


class FolderLimitError(FavError):
    """收藏夹达上限（建夹 -400）：停止收藏阶段，保留断点。"""


class BiliFavClient:
    """B 站收藏客户端（AsyncClient 外部注入）。

    Args:
        client: httpx.AsyncClient（测试用 respx mock）。
        db: Database 实例（收藏状态落盘，断点续跑）。
        config: Config 实例（收藏并发/间隔/拆分参数）。
        cookie_str: 解密后的 B 站 cookie 串（含 SESSDATA/bili_jct/buvid3）。
        retry_delays_s: 412 单请求指数退避间隔序列（文档 §7 响应层 a）。
        breaker: 全局熔断器（文档 §7 响应层 b）；None 时熔断关闭。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        db: Database,
        config: Config,
        cookie_str: str,
        *,
        user_agent: str | None = None,
        retry_delays_s: list[float] | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._db = db
        self._config = config
        self._cookies = _parse_cookies(cookie_str)
        self._csrf = self._cookies.get("bili_jct", "")
        self._breaker = breaker
        # 身份层（文档 §7）：UA 由调用方启动时从 config.http.user_agents 随机
        # 选定并全程固定；未传参时用默认浏览器 UA 兜底
        self._user_agent = user_agent or _DEFAULT_BROWSER_UA
        # up_mid：优先从 cookie DedeUserID 解析，nav 兜底；缓存复用
        self._mid: int | None = self._mid_from_cookie(self._cookies)
        # 文档 §7 响应层 a：指数退避间隔 2s→4s→8s（参数来自 config.retry）
        self._retry_delays_s = retry_delays_s or backoff_delays(config.risk_control.retry)
        # -702 限流退避：基数 2 倍起 4s→8s→16s（max 3 次）
        init = config.risk_control.retry.initial_delay_s
        self._rate_limit_delays_s = [init * 2 * (config.risk_control.retry.backoff_factor ** i) for i in range(config.risk_control.retry.max_retries)]  # noqa: E501
        # 自适应降速状态：连续 -702 计数 → interval 乘数（上限 4 倍）
        self._consecutive_702: int = 0
        self._slowdown_multiplier: float = 1.0
        # F3-6（§5.2/§8）：bvid→aid 进程内内存缓存——仅缓存 miss 发 view 请求，
        # 收藏请求量回到 N(deal)+M(view miss)。取舍：aid 为视频 immutable 标
        # 识（不随时间变化），进程生命周期内无需失效；不做落盘——阶段三单进程
        # 连续执行，跨进程复用收益小（每进程至多每个 bvid 一次 view），而落盘
        # 需引入 TTL/失效策略与额外 DDL，收益不抵复杂度。
        self._aid_cache: dict[str, int] = {}

    @property
    def concurrency_multiplier(self) -> float:
        """F3-7（§7 响应层 b）：critical 熔断后降并发倍率（0.5），无熔断器 1.0。"""
        if self._breaker is None:
            return 1.0
        return self._breaker.concurrency_multiplier

    @property
    def slowdown_multiplier(self) -> float:
        """-702 自适应降速倍率（§7 响应层 b：连续 2 次 -702 翻倍，上限 4x）。

        F4-5：fav_songs worker 经此公开属性读取，不再访问客户端私有属性。
        """
        return self._slowdown_multiplier

    @staticmethod
    def _mid_from_cookie(cookies: dict[str, str]) -> int | None:
        """从 cookie 的 DedeUserID 解析当前登录用户 mid。"""
        raw = cookies.get("DedeUserID")
        if raw and str(raw).isdigit():
            return int(raw)
        return None

    # ---- 请求基础 -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """与搜索一致的 session UA + Referer（文档 §7 身份层）。"""
        headers = {
            "User-Agent": self._user_agent,
            "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
            "Referer": "https://www.bilibili.com",
        }
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        return headers

    async def _resolve_mid(self) -> int:
        """确保得到当前登录用户 mid：cookie DedeUserID → nav 接口 data.mid 兜底。"""
        if self._mid is not None:
            return self._mid
        # nav 兜底（文档 §5.2 接口；未登录时 code=-101 但 data.mid 可用于有 cookie 场景）
        data = await self._request("GET", _NAV_URL)
        nav_data = data.get("data") or {}
        mid = nav_data.get("mid")
        if isinstance(mid, int) and mid > 0:
            self._mid = mid
            return mid
        raise AuthExpiredError(
            "无法确定当前登录用户 mid（DedeUserID 缺失且 nav 未返回 mid），请重新 `python main.py auth`"
        )

    async def _request(
        self, method: str, url: str,
        *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
    ) -> dict:
        """统一请求层：指数退避（§7 a）+ 全局熔断（§7 b）+ code 分类。

        - HTTP 状态码 412 → 走风控层（熔断记录 + 等待 + 重试）；
        - HTTP 网络/非 2xx → 指数退避重试，耗尽抛 FavError；
        - API code -412 → 同上风控处理（2s→4s→8s）；
        - API code -702 → 目标账号写限流：与 -412 同级，但退避 4s→8s→16s，
          计入全局熔断；退避期间不计失败，重试耗尽才抛 FavError；同时累计
          连续 -702 触发自适应降速（interval 翻倍，上限 4x）；
        - API code -101/-111 → AuthExpiredError 立即中止（§9.2，不重试）；
        - API code 非 0（如 -400 参数错误）→ 打印完整 body（不吞掉），交由调用方分类。
        """
        attempts = [0.0, *self._retry_delays_s]
        rate_attempts = [0.0, *self._rate_limit_delays_s]
        last_error: Exception | None = None
        # 区分当前尝试用的是哪种退避序列（-702 限流走 rate 分支）
        rate_backoff = False
        for attempt, delay in enumerate(attempts):
            if attempt > 0:
                step = rate_attempts[attempt] if rate_backoff else delay
                if rate_backoff:
                    logger.info(
                        "目标账号限流，退避 %.0fs 后重试（%s/%s）",
                        step, attempt, len(self._rate_limit_delays_s),
                    )
                await sleep_before_retry(step)
            try:
                resp = await self._client.request(
                    method, url, params=params, data=data, headers=self._headers()
                )
                # 先看 HTTP 状态码：412 走风控层
                if resp.status_code == 412:
                    raise _RiskError("HTTP 412")
                resp.raise_for_status()
                data_json = resp.json()
            except _RiskError:
                await self._trip_breaker()
                last_error = FavError(f"HTTP 412（风控，尝试 {attempt + 1}）")
                if attempt == len(attempts) - 1:
                    raise FavError(f"请求失败（重试耗尽）: {url}") from last_error
                continue
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == len(attempts) - 1:
                    raise FavError(f"请求失败（重试耗尽）: {url}") from last_error
                continue

            code = data_json.get("code")
            if code in AUTH_EXPIRED_CODES:
                # 文档 §9.2：凭证失效立即中止，不做退避重试
                raise AuthExpiredError(
                    f"{url}：API code {code}（凭证失效），请执行 `python main.py auth` 重新授权"
                )
            if code == -412:
                await self._trip_breaker()
                last_error = FavError(f"API code -412（风控，尝试 {attempt + 1}）")
                if attempt == len(attempts) - 1:
                    raise FavError(f"请求失败（重试耗尽）: {url}") from last_error
                continue
            if code == RATE_LIMIT_CODE:
                # 目标账号写限流（-702）：计入熔断 + 自适应降速
                await self._trip_breaker()
                self._record_rate_limit()
                rate_backoff = True
                last_error = FavError(f"API code {RATE_LIMIT_CODE}（账号限流，尝试 {attempt + 1}）")
                if attempt == len(attempts) - 1:
                    raise FavError(f"请求失败（限流重试耗尽）: {url}") from last_error
                continue
            # 成功或业务错误：重置连续限流计数
            self._consecutive_702 = 0
            if code != 0:
                # 参数/业务错误：打印完整 body，不得吞掉（便于诊断 -400 等）
                logger.error("接口返回非 0 code：%s %s → %s", method, url, data_json)
            return data_json
        raise FavError(f"请求失败: {url}") from last_error

    def _record_rate_limit(self) -> None:
        """累计连续 -702；达阈值后自适应降速（interval 翻倍，上限 4 倍）。"""
        self._consecutive_702 += 1
        if (
            self._consecutive_702 >= RATE_LIMIT_CONSECUTIVE_THRESHOLD
            and self._slowdown_multiplier < RATE_LIMIT_MAX_MULTIPLIER
        ):
            self._slowdown_multiplier = min(
                self._slowdown_multiplier * 2, RATE_LIMIT_MAX_MULTIPLIER
            )
            self._consecutive_702 = 0  # 重新计数
            logger.warning(
                "目标账号写限流偏严：剩余请求间隔 x%.1f（上限 x%.0f）",
                self._slowdown_multiplier, RATE_LIMIT_MAX_MULTIPLIER,
            )

    async def _trip_breaker(self) -> None:
        """熔断记录 + 等待暂停（文档 §7 响应层 b）。"""
        if self._breaker is not None:
            self._breaker.record_failure()
            await self._breaker.wait_if_paused()

    def _check_code(self, data: dict, *, context: str) -> dict:
        """统一 code 判定：-101/-111 中止、-400 建夹停止、其余返回由调用方处理。"""
        code = data.get("code")
        if code in AUTH_EXPIRED_CODES:
            raise AuthExpiredError(
                f"{context}：API code {code}（凭证失效），请执行 `python main.py auth` 重新授权"
            )
        if code == -400 and context == "建夹":
            raise FolderLimitError(
                "建夹失败（API code -400，收藏夹已达上限）——停止收藏阶段，保留断点"
            )
        return data

    # ---- 收藏夹拆分（文档 §5.3）------------------------------------

    async def list_folders(self) -> list[dict]:
        """查询我创建的收藏夹列表，返回 [{id: media_id, title: ...}]。

        - 必填参数 up_mid（当前登录用户 mid，诊断 2026-09-22：缺参返回失败）；
        - 走统一 _request 层：412 指数退避 + 熔断，不裸 raise_for_status。
        """
        mid = await self._resolve_mid()
        data = await self._request("GET", _FOLDER_LIST_URL, params={"up_mid": mid})
        data = self._check_code(data, context="查夹")
        return (data.get("data") or {}).get("list") or []

    async def create_folder(self, title: str) -> int:
        """创建收藏夹，返回 media_id。"""
        data = await self._request(
            "POST", _FOLDER_ADD_URL, data={"title": title, "privacy": 0, "csrf": self._csrf}
        )
        data = self._check_code(data, context="建夹")
        if data.get("code") != 0:
            raise FavError(f"建夹失败 {title}: code {data.get('code')}")
        return int(data["data"]["id"])

    def matched_song_ordinals(self, task_id: str) -> dict[str, int]:
        """F1-3（§5.3）/F4-5：按 song_key 排序查询任务全部已匹配歌曲，
        返回 {song_key: ordinal} 稳定分段映射（供 fav_songs 按序连续分段）。

        并发下歌曲插入序不确定，须显式按 song_key 排序保证同一首歌恒进原夹。
        """
        rows = self._db.query(
            "SELECT song_key FROM songs WHERE task_id = ? AND bvid IS NOT NULL "
            "AND status IN ('DONE','MATCHED','FAV_FAILED') ORDER BY song_key",
            (task_id,),
        )
        return {r["song_key"]: i for i, r in enumerate(rows)}

    async def ensure_folders(self, playlist_name: str, total: int) -> list[int]:
        """按 900/夹拆分并确保收藏夹存在，返回各夹 media_id 列表。

        重跑时按名称查询复用已有同名夹，不为同一任务重复建夹（文档 §5.3）。
        F4-5：total<=0（无可收藏歌）直接返回空列表，不发 list_folders 请求、
        不建空夹。
        """
        if total <= 0:
            return []
        limit = self._config.fav.per_folder_limit
        count = math.ceil(total / limit)
        existing = {f["title"]: int(f["id"]) for f in await self.list_folders()}
        media_ids: list[int] = []
        for index in range(1, count + 1):
            name = self._config.fav.name_template.format(playlist_name=playlist_name, index=index)
            if name in existing:
                media_ids.append(existing[name])
                logger.info("复用已有收藏夹 %s (media_id=%s)", name, existing[name])
            else:
                media_ids.append(await self.create_folder(name))
        return media_ids

    # ---- 幂等收藏三分支（文档 §9.3）---------------------------------

    async def _bvid_to_aid(self, bvid: str) -> int:
        """bvid → av 号（deal 接口的 rid 需要 aid）。

        走 /x/web-interface/view 只读接口；view 详情请求与阶段二同源，可复用。

        F3-6（§5.2/§8）：进程内内存缓存命中直接返回，仅 miss 发 view 请求。
        """
        cached = self._aid_cache.get(bvid)
        if cached is not None:
            return cached
        data = await self._request(
            "GET", _VIEW_URL, params={"bvid": bvid}
        )
        aid = (data.get("data") or {}).get("aid")
        if not isinstance(aid, int):
            raise FavError(f"bvid→aid 转换失败: {bvid}")
        self._aid_cache[bvid] = aid
        return aid

    async def add_resource(self, media_id: int, bvid: str) -> dict:
        """向收藏夹添加视频资源，返回响应 JSON。

        deal 接口 rid 需要 av 号（文档：rid 为稿件 avid，实测 2026-09-22），
        先经 bvid→aid 转换。
        """
        aid = await self._bvid_to_aid(bvid)
        data = await self._request(
            "POST",
            _RESOURCE_ADD_URL,
            data={
                "rid": aid,
                "type": 2,
                "add_media_ids": media_id,
                "del_media_ids": "",
                "csrf": self._csrf,
            },
        )
        return self._check_code(data, context="收藏")

    async def fav_one(self, media_id: int, song: dict) -> dict:
        """收藏单首歌，按文档 §9.3 幂等分支返回结果。

        Returns:
            {"status": "DONE" | "MANUAL" | "FAV_FAILED", "bvid": ..., "fail_reason": ...}
            非 -404/62002 失败不回 DONE，置 FAV_FAILED（resume 只重跑阶段三，§4.4）。
        """
        key = f"{song.get('name')}|{song.get('artist')}"
        task_id = song.get("task_id") or "default"  # 兼容无任务字段的老调用
        bvid = song.get("bvid")
        try:
            data = await self.add_resource(media_id, bvid)
        except (AuthExpiredError, FolderLimitError):
            raise  # 文档 §9.2/-400：中止语义不变（不吞）
        except FavError as exc:
            # 重试耗尽（含 -702 限流耗尽、网络重试耗尽）→ 置 FAV_FAILED 记因继续
            reason = f"收藏失败（{exc}）"
            self._db.upsert_song(
                key, task_id=task_id, status="FAV_FAILED", fail_reason=reason
            )
            logger.warning("收藏失败 %s：%s", key, reason)
            return {"status": "FAV_FAILED", "bvid": bvid, "fail_reason": reason}
        code = data.get("code")

        if code == 0:
            # code 0：收藏成功（含重复添加的幂等成功，文档 §9.3）。
            # F3-2/F4-5：upsert_song 转 DONE 时强制 fail_reason=NULL（db 白名单
            # 语义），无需再手动 UPDATE 清因
            self._db.upsert_song(
                key, task_id=task_id, status="DONE", method=song.get("method"), bvid=bvid
            )
            logger.info("收藏成功 %s → %s", key, bvid)
            return {"status": "DONE", "bvid": bvid}

        if code in VIDEO_NOT_FOUND_CODES:
            reason = f"视频不存在/被删（code {code}）"
            self._db.upsert_song(
                key, task_id=task_id, status="MANUAL",
                method=song.get("method"), fail_reason=reason,
            )
            logger.warning("候选视频已失效，转入人工队列 %s：%s", key, reason)
            return {"status": "MANUAL", "bvid": bvid, "fail_reason": reason}

        reason = f"收藏失败（code {code}）"
        # 文档 §9.3：非 -404/62002 失败 → FAV_FAILED（不回 DONE，保留 bvid 供 resume 重试）
        self._db.upsert_song(
            key, task_id=task_id, status="FAV_FAILED", fail_reason=reason
        )
        logger.warning("收藏失败 %s：%s", key, reason)
        return {"status": "FAV_FAILED", "bvid": bvid, "fail_reason": reason}


def _parse_cookies(cookie_str: str) -> dict[str, str]:
    """宽松解析 cookie 串（仅需 bili_jct，不要求三件套齐全）。"""
    result: dict[str, str] = {}
    for part in cookie_str.replace(",", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        result[name.strip()] = value.strip()
    return result


async def fav_songs(
    client: BiliFavClient,
    config: Config,
    songs: list[dict],
    playlist_name: str,
    *,
    media_ids: list[int] | None = None,
) -> dict:
    """批量收藏（文档 §7 收藏参数 + §5.3 按序连续分段）。

    F1-3（§5.3）：收藏夹分配改按序连续分段（第 i 首进夹 ⌊i/per_folder_limit⌋），
    禁止轮转；resume 建夹数量按任务总匹配数计算，保证歌进原夹。断点映射按
    song_key 排序定序（并发插入序不确定，须显式排序稳定映射）。

    F4-5：去掉死参数 db——ordinal 映射经 client.matched_song_ordinals() 公开
    方法读取，降速倍率经 client.slowdown_multiplier 公开属性读取。

    Args:
        media_ids: 预分配的收藏夹 media_id 列表；None 时按任务总匹配数自动建夹。
    """
    if not songs:
        return {"total": 0, "statuses": {}}

    per_folder_limit = config.fav.per_folder_limit
    task_id = songs[0].get("task_id") or "default"

    # F1-3（§5.3）：按 song_key 排序查询全任务匹配数，建立稳定 ordinal 映射。
    # 并发下 songs 插入序不确定，须显式排序保证断点映射稳定（同一首歌恒进原夹）。
    ordinal = client.matched_song_ordinals(task_id)
    total_matched = len(ordinal)

    if media_ids is None:
        # F1-3（§5.3）：resume 建夹数量按任务总匹配数（非仅本次待收藏数），
        # 保证复用原有同名夹、每首歌回到原属分段夹；total_matched 为 0 时（DB 未落盘）
        # 回退到本次传入数（兼容测试/非标准调用）。
        media_ids = await client.ensure_folders(
            playlist_name, total_matched or len(songs)
        )

    # 按序连续分段（禁止轮转）：第 i 首进夹 ⌊ordinal/per_folder_limit⌋，封顶末夹
    assigned = []
    for i, song in enumerate(songs):
        key = song.get("song_key") or f"{song.get('name')}|{song.get('artist')}"
        pos = ordinal.get(key, i)  # DB 未命中时回退本次序号（防御）
        folder_idx = min(pos // per_folder_limit, len(media_ids) - 1)
        assigned.append((media_ids[folder_idx], song))

    rl = config.rate_limit.fav
    sem = asyncio.Semaphore(rl.concurrency)

    async def worker(media_id: int, song: dict) -> dict:
        async with sem:
            # 文档 §7 预防层 + -702 自适应降速（interval × 客户端乘数，上限 4x）
            # F3-7（§7 响应层 b）：再除以熔断降并发倍率——critical 后
            # multiplier=0.5，间隔再翻倍；与 slowdown_multiplier 相乘叠加：
            # 最终间隔 = base × slowdown × 1/concurrency_multiplier。
            interval_ms = (
                rl.interval_ms
                * client.slowdown_multiplier
                / client.concurrency_multiplier
            )
            jitter_lo, jitter_hi = rl.jitter_ms
            await asyncio.sleep(random.uniform(interval_ms + jitter_lo, interval_ms + jitter_hi) / 1000)
            return await client.fav_one(media_id, song)

    tasks = [asyncio.create_task(worker(mid, song)) for mid, song in assigned]
    results = await asyncio.gather(*tasks)

    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {"total": len(songs), "statuses": statuses}


__all__ = [
    "AUTH_EXPIRED_CODES",
    "AuthExpiredError",
    "BiliFavClient",
    "FavError",
    "FolderLimitError",
    "VIDEO_NOT_FOUND_CODES",
    "fav_songs",
]
