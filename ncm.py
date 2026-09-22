"""网易云模块（里程碑 M1，对应文档 §5.1）。

NcmClient 提供：
- fetch_playlist_track_ids：playlist/detail 按 n=1000&offset= 分页，只依赖 trackIds；
- fetch_song_details：song/detail POST 批量取详情（每批 ≤1000）；
- fetch_playlist_name：歌单名（收藏夹命名用）。

设计约束：
- httpx.AsyncClient 由外部注入（构造参数），便于测试 respx mock；
- 请求失败（HTTP 非 200 / API code 非 200 / 网络错误）重试 N 次后抛 NcmError；
- 网易云接口无需登录。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

# 文档 §5.1 接口
_BASE = "https://music.163.com"
_PLAYLIST_DETAIL = f"{_BASE}/api/v6/playlist/detail"
_SONG_DETAIL = f"{_BASE}/api/v3/song/detail"

# 文档 §5.1：分页上限与每批上限
_DEFAULT_MAX_TRACKS = 3000
_BATCH_SIZE = 1000


class NcmError(Exception):
    """网易云请求在重试耗尽后仍失败。"""


class NcmClient:
    """网易云接口客户端（AsyncClient 外部注入）。

    Args:
        client: httpx.AsyncClient 实例（由调用方创建/关闭，测试用 respx mock）。
        retries: 失败后的重试次数（验收语义：失败重试 2 次后抛出）。
        retry_delay_s: 重试间隔（默认 0，测试不等待）。
        headers: 附加请求头（如 User-Agent），合并到每次请求。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retries: int = 2,
        retry_delay_s: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._retries = retries
        self._retry_delay_s = retry_delay_s
        self._headers = dict(headers or {})

    # ---- 请求基础 -------------------------------------------------

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> dict:
        """发送请求并返回 JSON，失败重试 self._retries 次后抛 NcmError。"""
        kwargs.setdefault("headers", self._headers)
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt > 0 and self._retry_delay_s > 0:
                await asyncio.sleep(self._retry_delay_s)
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code != 200:
                    raise NcmError(f"HTTP {resp.status_code} (尝试 {attempt + 1})")
                data = resp.json()
                if data.get("code") not in (None, 200):
                    raise NcmError(f"API code {data.get('code')} (尝试 {attempt + 1})")
                return data
            except httpx.HTTPError as exc:
                last_error = exc
            except NcmError as exc:
                last_error = exc
        raise NcmError(f"请求失败（重试 {self._retries} 次后仍失败）: {url}") from last_error

    # ---- 歌单/详情（文档 §5.1）-------------------------------

    async def fetch_playlist_track_ids(
        self,
        playlist_id: int,
        *,
        max_tracks: int = _DEFAULT_MAX_TRACKS,
        page_size: int = _BATCH_SIZE,
    ) -> list[int]:
        """分页拉取歌单全部 trackIds（n=1000&offset= 直至取完）。

        - 只依赖响应中的 trackIds 字段（文档 §5.1：tracks 可能不完整）；
        - 返回按歌单顺序拼接的 id 列表，最多 max_tracks 首。
        """
        track_ids: list[int] = []
        offset = 0
        while True:
            data = await self._request_json(
                "GET",
                _PLAYLIST_DETAIL,
                params={"id": playlist_id, "n": page_size, "offset": offset},
            )
            playlist = data.get("playlist") or {}
            page_ids = [
                item["id"]
                for item in playlist.get("trackIds") or []
                if isinstance(item, dict) and item.get("id") is not None
            ]
            track_ids.extend(page_ids)
            # 一页不足（已取完）或达到上限则停止
            if len(page_ids) < page_size or len(track_ids) >= max_tracks:
                break
            offset += page_size
        return track_ids[:max_tracks]

    async def fetch_playlist_name(self, playlist_id: int) -> str:
        """获取歌单名（收藏夹命名用，文档 §5.3："<歌单名> (n)"）。"""
        data = await self._request_json(
            "GET", _PLAYLIST_DETAIL, params={"id": playlist_id, "n": 1, "offset": 0}
        )
        playlist = data.get("playlist") or {}
        return playlist.get("name") or f"歌单{playlist_id}"

    async def fetch_song_details(self, song_ids: list[int]) -> list[dict]:
        """批量取歌曲详情，每批 ≤1000（文档 §5.1）。

        Returns:
            每首歌解析后的 dict：ncm_id / name / artist / album / alia / origin
            （origin 为 originSongSimpleData 原文，可能是 None）。
        """
        results: list[dict] = []
        for start in range(0, len(song_ids), _BATCH_SIZE):
            batch = song_ids[start:start + _BATCH_SIZE]
            payload = {"c": json.dumps([{"id": sid} for sid in batch])}
            data = await self._request_json("POST", _SONG_DETAIL, data=payload)
            results.extend(self._parse_songs(data.get("songs") or []))
        return results

    # ---- 字段解析 -------------------------------------------------

    @staticmethod
    def _parse_songs(songs: list[dict]) -> list[dict]:
        parsed: list[dict] = []
        for song in songs:
            parsed.append(
                {
                    "ncm_id": song.get("id"),
                    "name": song.get("name"),
                    "artist": "/".join(
                        ar.get("name", "") for ar in (song.get("ar") or []) if isinstance(ar, dict)
                    ),
                    "album": (song.get("al") or {}).get("name"),
                    "alia": song.get("alia") or [],          # JSON 数组
                    "origin": song.get("originSongSimpleData"),  # 翻唱原曲信息
                }
            )
        return parsed

    # ---- 生命周期（代理注入的 client）------------------------------

    async def __aenter__(self) -> NcmClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)


__all__ = ["NcmClient", "NcmError"]
