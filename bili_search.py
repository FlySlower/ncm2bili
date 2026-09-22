"""B 站搜索模块（里程碑 M2，对应文档 §5.2；§7/§9.1 风控语义对齐 v0.4.1）。

BiliSearchClient 提供：
- WBI 签名（get_mixin_key / sign_wbi，算法与 bilibili-API-collect 公开实现逐行对应）；
- nav 取 img_key/sub_key，缓存在内存 + kv_meta 表（TTL 1 天），-403 时强制刷新重试一次；
- buvid3 预取：SPI 主路径 / 主页 Set-Cookie 降级，随搜索请求携带。

风控语义（文档 §7 响应层 / §9.1）：
- HTTP 412（WAF openresty text/html，body 非 JSON）按 -412 计入全局熔断（响应层 b）；
- API code -412 同级入熔断；熔断触发后 wait_if_paused 全局冷却；
- 单请求指数退避 2s→4s→8s（响应层 a）：网络错误 / 412 / 非 0 code 都经退避重试；
- 每次重试重新生成 wts/w_rid（§9.1：签名绑死 wts，原样重发无效）；
- BiliError 消息携带最后响应的 HTTP 状态码与根因异常（repr），供单歌降级记录。

成功判定：B 站 API code == 0（区别于网易云的 200，见文档 §5.2 错误码约定）。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from backoff import sleep_before_retry
from circuit_breaker import CircuitBreaker
from db import Database

# 文档 §5.2 接口
_BASE = "https://api.bilibili.com"
_NAV_URL = f"{_BASE}/x/web-interface/nav"
_SEARCH_URL = f"{_BASE}/x/web-interface/wbi/search/type"
_HOME_URL = "https://www.bilibili.com/"
# 文档 §5.2 buvid3 获取：
# - 主路径：SPI 指纹接口（取 data.b_3 / data.b_4）
# - 降级路径：主页 Set-Cookie
# - credentials 表持久化复用（key = "buvid3"）
_SPI_URL = f"{_BASE}/x/frontend/finger/spi"
_KV_BUVID3 = "buvid3"
# 降级路径 UA 断言/修正用默认浏览器 UA（不得含 python/curl/httpx 子串）
_DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# 文档 §5.2 第 2 步：重排映射表（64 项，来自 bilibili-API-collect 公开资料）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# kv_meta 中 WBI key 的存储 key
_KV_WBI_KEY = "wbi_keys"


class BiliError(Exception):
    """B 站请求失败。"""


class WbiKeyError(BiliError):
    """API code -403：WBI 签名/时间戳错误，需强制刷新 key 后重试。"""


def get_mixin_key(orig: str) -> str:
    """文档 §5.2 第 2 步：按 MIXIN_KEY_ENC_TAB 重排 img_key+sub_key，截取前 32 位。"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    wts: int | None = None,
) -> dict[str, Any]:
    """文档 §5.2 第 3~5 步：为请求参数计算 w_rid 并返回签名后的参数字典。

    对应公开参考实现 encWbi：
    1. mixin_key = getMixinKey(img_key + sub_key)          # 第 2 步
    2. params["wts"] = round(time.time())                   # 第 3 步
    3. params = dict(sorted(params.items()))                # 第 4 步：按 key 升序
    4. 过滤 value 中 "!'()*" 字符                            # 公开实现为过滤而非转义
    5. query = urlencode(params)                            # 第 4 步：url query 编码
    6. w_rid = md5(query + mixin_key).hexdigest()           # 第 5 步
    """
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = wts if wts is not None else round(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in params.items()
    }
    query = urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


class BiliSearchClient:
    """B 站搜索客户端（AsyncClient 由外部注入）。

    Args:
        client: httpx.AsyncClient 实例（测试用 respx mock）。
        db: Database 实例，用于 kv_meta 缓存 img_key/sub_key；None 时仅内存缓存。
        retries: 网络/HTTP 错误重试次数（默认 2，配合指数退避共 3 次尝试）。
        retry_delays_s: 单请求指数退避间隔序列（文档 §7 响应层 a）；
                        默认 2s→4s→8s，与 fav 共用 backoff 语义。
        wbi_ttl_s: WBI key 缓存 TTL（默认 1 天，文档 §5.2）。
        headers: 附加请求头（如 UA），合并到每次请求。
        breaker: 全局熔断器（文档 §7 响应层 b）；None 时熔断关闭。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        db: Database | None = None,
        *,
        retries: int = 2,
        retry_delays_s: list[float] | None = None,
        wbi_ttl_s: int = 86400,
        headers: dict[str, str] | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._db = db
        self._retries = retries
        # 文档 §7 响应层 a：指数退避间隔（与 fav 相同序列，参数可注入）
        self._retry_delays_s = retry_delays_s or [2.0, 4.0, 8.0]
        self._wbi_ttl_s = wbi_ttl_s
        self._headers = dict(headers or {})
        self._breaker = breaker
        self._img_key: str | None = None
        self._sub_key: str | None = None
        self._buvid3: str | None = None
        self._buvid4: str | None = None

    # ---- 请求基础（B 站语义：code==0 成功）-----------------------

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        accept_codes: tuple[int, ...] = (),
        build_kwargs: Callable[[], dict] | None = None,
        **kwargs: Any,
    ) -> dict:
        """发请求并按 B 站语义判定成功（code==0，或 accept_codes 中显式放行）。

        accept_codes: 除 0 外仍视为"可接受响应"的 API code（如 nav 未登录
        的 -101：data.wbi_img 照常返回，签名不依赖登录态）。

        build_kwargs: 每次尝试前重新构造请求参数（返回完整 kwargs）。搜索路径
        用它在每次重试时重新调用 sign_wbi 生成新的 wts/w_rid（文档 §9.1：
        签名绑死 wts，原样重发对 412 无效）；为 None 时所有尝试复用同一参数。

        重试语义（文档 §7 响应层 a）：网络错误 / HTTP 412（含 WAF）/ 非 0 API
        code 在指数退避后重试，重试耗尽抛 BiliError——消息携带最后 HTTP 状态码
        与根因异常，供 matcher 单歌降级（SEARCH_FAILED）记录。
        """
        kwargs.setdefault("headers", self._headers)
        last_status: int | None = None
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt > 0:
                # 文档 §7 响应层 a：单请求指数退避（2s→4s→8s，与 fav 共用）
                await sleep_before_retry(self._retry_delays_s[attempt - 1])
            call_kwargs = build_kwargs() if build_kwargs is not None else kwargs
            try:
                resp = await self._client.request(method, url, **call_kwargs)
                last_status = resp.status_code
                return await self._classify_response(resp, accept_codes, attempt)
            except httpx.HTTPError as exc:
                last_error = exc
            except WbiKeyError:
                raise  # -403 不重试，由调用方强制刷新 key
            except BiliError as exc:
                last_error = exc
        raise BiliError(
            f"请求失败（重试 {self._retries} 次后仍失败）: {url} "
            f"[最后 HTTP 状态码 {last_status}; 根因 "
            f"{type(last_error).__name__}: {last_error}]"
        ) from last_error

    async def _classify_response(
        self, resp: httpx.Response, accept_codes: tuple[int, ...], attempt: int
    ) -> dict:
        """把一次响应归类为成功 / 风控（-412、WAF 412）/ -403 / 其他 BiliError。

        - HTTP 412 且 body 非 JSON（openresty text/html，WAF 层拦截）→ 按 -412
          计入全局熔断（文档 §7 响应层 b，v0.4.1：此前仅认 body 的 API code，
          WAF 412 永不计数导致全局暂停失效）；
        - API code -412 同级入熔断（-702 目标账号写限流由 fav 侧独立处理）；
        - API code -403 → WbiKeyError（强制刷新 key 后重试一次，§5.2）。
        """
        if resp.status_code == 412:
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = None
            if not isinstance(body, dict) or "code" not in body:
                # WAF 412：无 JSON body → 按 -412 计数（与 body code 同义）
                if self._breaker is not None:
                    self._breaker.record_failure()
                    await self._breaker.wait_if_paused()
                raise BiliError(f"HTTP 412（WAF 风控，尝试 {attempt + 1}）")
            # 带 JSON body 的 412 沿用 API code 语义（code == -412 最常见）
            data = body
        else:
            resp.raise_for_status()
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise BiliError(f"响应非 JSON（HTTP {resp.status_code}，尝试 {attempt + 1}）") from exc
        code = data.get("code")
        if code == 0 or code in accept_codes:
            return data
        if code == -412:
            # 文档 §7 响应层 b：全局熔断（记录并等待暂停结束）
            if self._breaker is not None:
                self._breaker.record_failure()
                await self._breaker.wait_if_paused()
            raise BiliError(f"API code -412（风控，尝试 {attempt + 1}）")
        if code == -403:
            raise WbiKeyError(f"API code -403（尝试 {attempt + 1}）")
        raise BiliError(f"API code {code}（尝试 {attempt + 1}）")

    # ---- WBI key：内存 + kv_meta 缓存（文档 §5.2）------------------

    async def _ensure_wbi_keys(self, *, force: bool = False) -> tuple[str, str]:
        """取 img_key/sub_key：内存 → kv_meta（TTL 内）→ nav 请求，并回写缓存。"""
        if not force and self._img_key and self._sub_key:
            return self._img_key, self._sub_key

        if not force and self._db is not None:
            row = self._db.query_one(
                "SELECT value, fetched_at FROM kv_meta WHERE key = ?", (_KV_WBI_KEY,)
            )
            if row is not None and (time.time() - row["fetched_at"]) < self._wbi_ttl_s:
                cached = json.loads(row["value"])
                self._img_key, self._sub_key = cached["img_key"], cached["sub_key"]
                return self._img_key, self._sub_key

        img_key, sub_key = await self._fetch_wbi_keys()
        self._img_key, self._sub_key = img_key, sub_key
        if self._db is not None:
            self._db.execute(
                "INSERT INTO kv_meta (key, value, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
                (_KV_WBI_KEY, json.dumps({"img_key": img_key, "sub_key": sub_key}), int(time.time())),
            )
        return img_key, sub_key

    async def _fetch_wbi_keys(self) -> tuple[str, str]:
        """文档 §5.2 第 1 步：nav 接口取 img_url/sub_url，提取文件名作为 key。

        nav 未登录时 API code 为 -101（账号未登录），但 data.wbi_img 照常
        返回；WBI 签名与登录态无关，故 -101 也可接受，不视为失败
        （与 bilibili-API-collect 公开实现一致：不校验 nav 的 code）。
        """
        data = await self._request_json("GET", _NAV_URL, accept_codes=(-101,))
        wbi_img = data["data"]["wbi_img"]
        img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        return img_key, sub_key

    # ---- buvid3 预取（文档 §5.2）-----------------------------------

    async def ensure_buvid3(self, *, force: bool = False) -> str:
        """确保 buvid3 可用（文档 §5.2）。

        主路径：SPI 指纹接口 GET /x/frontend/finger/spi，取 data.b_3 写入
        buvid3、data.b_4 存为 buvid4 备用；
        降级路径：spi 失败（网络错误/非 0 code）时回退主页 Set-Cookie 抓取，
        抓取前断言 session UA 为浏览器 UA（不含 python/curl/httpx）；
        两条路径都失败 → 抛带指引的异常，禁止静默继续。
        成功后持久化到 credentials 表，重跑直接复用（零请求）。
        """
        if self._buvid3 and not force:
            return self._buvid3

        # 请求前统一确保浏览器 UA（文档 §5.2 第 2 条：断言 UA 不含
        # python/curl/httpx，不满足则先修正），SPI 与主页路径均受益
        self._ensure_browser_ua()

        # 复用持久化的 buvid3（文档 §5.2：重跑直接复用，不必每次重新获取）
        if not force and self._db is not None:
            row = self._db.query_one(
                "SELECT value FROM credentials WHERE key = ?", (_KV_BUVID3,)
            )
            if row is not None and row["value"]:
                self._buvid3 = row["value"]
                return self._buvid3

        # 主路径：SPI 指纹接口
        try:
            data = await self._request_json("GET", _SPI_URL)
            b3 = data["data"]["b_3"]
            if not b3:
                raise BiliError("SPI 未返回 b_3")
            self._buvid3 = b3
            self._buvid4 = data["data"].get("b_4")
            self._persist_buvid3()
            return self._buvid3
        except (BiliError, httpx.HTTPError, KeyError, TypeError):
            pass  # 降级到主页 Set-Cookie

        # 降级路径：主页 Set-Cookie（UA 已在开头修正）
        try:
            resp = await self._client.get(_HOME_URL, headers=self._headers)
            for cookie in resp.headers.get_list("set-cookie"):
                name, _, rest = cookie.partition("=")
                if name.strip().lower() == "buvid3":
                    self._buvid3 = rest.split(";")[0].strip()
                    self._persist_buvid3()
                    return self._buvid3
        except httpx.HTTPError:
            pass

        raise BiliError(
            "buvid3 获取失败：SPI 与主页 Set-Cookie 两条路径均未取到。"
            "请检查网络连接，并确认 BiliSearchClient 已注入浏览器 UA（不含 "
            "python/curl/httpx 子串），必要时修正后重试。"
        )

    def _ensure_browser_ua(self) -> dict[str, str]:
        """断言 session UA 已配置且不含 python/curl/httpx；不满足则修正（文档 §5.2）。"""
        headers = dict(self._headers)
        ua = headers.get("User-Agent") or headers.get("user-agent") or ""
        if not ua or any(tok in ua.lower() for tok in ("python", "curl", "httpx")):
            headers["User-Agent"] = _DEFAULT_BROWSER_UA
            self._headers = headers  # 修正后写回，后续搜索请求同样受益
        return headers

    def _persist_buvid3(self) -> None:
        """buvid3 成功后持久化到 credentials 表（文档 §5.2 第 4 条）。"""
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_KV_BUVID3, self._buvid3, int(time.time())),
        )

    def _search_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        cookies = []
        if self._buvid3:
            cookies.append(f"buvid3={self._buvid3}")
        if self._buvid4:
            cookies.append(f"buvid4={self._buvid4}")
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    # ---- 搜索 -------------------------------------------------------

    async def search_videos(self, keyword: str, *, page: int = 1) -> dict:
        """按关键词搜索视频（文档 §5.2），返回响应 data（含 result 候选列表）。

        - 请求前确保 buvid3 与 WBI key；
        - 遇到 -403 时强制刷新 key 并重试一次（文档 §5.2）。
        """
        await self.ensure_buvid3()
        params = {"search_type": "video", "keyword": keyword, "page": page}
        try:
            return await self._search_with_wbi(params)
        except WbiKeyError:
            await self._ensure_wbi_keys(force=True)
            return await self._search_with_wbi(params)

    async def _search_with_wbi(
        self, params: dict[str, Any], img_key: str | None = None, sub_key: str | None = None
    ) -> dict:
        """带 WBI 签名搜索；每次重试通过 build_kwargs 重新调用 sign_wbi。

        文档 §9.1：签名绑死 wts，原样重发对风控无效——重试必须重新生成
        wts/w_rid。由 _request_json 在每次尝试前回调 build_kwargs 实现。
        """
        if img_key is None or sub_key is None:
            img_key, sub_key = await self._ensure_wbi_keys()

        def _build_kwargs() -> dict:
            signed = sign_wbi(params, img_key, sub_key)  # 每次尝试重新生成 wts/w_rid
            return {"params": signed, "headers": self._search_headers()}

        data = await self._request_json("GET", _SEARCH_URL, build_kwargs=_build_kwargs)
        return data.get("data") or {}

    # ---- 生命周期 ---------------------------------------------------

    async def __aenter__(self) -> BiliSearchClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)


__all__ = [
    "BiliSearchClient",
    "BiliError",
    "WbiKeyError",
    "get_mixin_key",
    "sign_wbi",
    "MIXIN_KEY_ENC_TAB",
]
