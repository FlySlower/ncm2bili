"""ncm.py 单测（里程碑 M1）：全部使用 respx mock，禁止真实网络请求。

对应文档 §5.1：playlist/detail 分页 / song/detail 批量 / song/wiki/summary。
"""
from __future__ import annotations

import json
from urllib.parse import unquote_plus

import httpx
import pytest
import respx

from ncm import NcmClient, NcmError

PLAYLIST_URL = "https://music.163.com/api/v6/playlist/detail"
SONG_DETAIL_URL = "https://music.163.com/api/v3/song/detail"
WIKI_URL = "https://music.163.com/api/song/wiki/summary"


def _make_playlist_page(track_ids: list[int], n_tracks: int = 0) -> dict:
    """构造 playlist/detail 响应：trackIds 完整，tracks 仅前 n_tracks 首（模拟文档 §5.1 不完整情况）。"""
    return {
        "code": 200,
        "playlist": {
            "id": 123,
            "name": "测试歌单",
            "tracks": [{"id": i} for i in track_ids[:n_tracks]],
            "trackIds": [{"id": i, "v": 1} for i in track_ids],
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_playlist_pagination_2500() -> None:
    """验收 1：2500 首分页 → playlist/detail 调用 3 次，trackIds 完整拼接。"""
    all_ids = list(range(1, 2501))
    for offset in (0, 1000, 2000):
        respx.get(PLAYLIST_URL, params={"id": "123", "n": "1000", "offset": str(offset)}).mock(
            return_value=httpx.Response(200, json=_make_playlist_page(all_ids[offset:offset + 1000]))
        )

    client = NcmClient(httpx.AsyncClient())
    async with client:
        ids = await client.fetch_playlist_track_ids(123)

    assert ids == all_ids
    # 恰好 3 次分页请求
    assert len(respx.calls) == 3
    for call in respx.calls:
        assert str(call.request.url) == (
            "https://music.163.com/api/v6/playlist/detail?n=1000&id=123&offset="
        ) or str(call.request.url).startswith(PLAYLIST_URL)


@pytest.mark.asyncio
@respx.mock
async def test_relies_on_track_ids_when_tracks_incomplete() -> None:
    """验收 2：tracks 字段不完整（仅 2 首）但 trackIds 完整时，只依赖 trackIds。"""
    all_ids = list(range(1, 2501))
    for offset in (0, 1000, 2000):
        page = all_ids[offset:offset + 1000]
        # tracks 每页只给 2 首，trackIds 给满 1000
        respx.get(PLAYLIST_URL, params={"id": "456", "n": "1000", "offset": str(offset)}).mock(
            return_value=httpx.Response(200, json=_make_playlist_page(page, n_tracks=2))
        )

    client = NcmClient(httpx.AsyncClient())
    async with client:
        ids = await client.fetch_playlist_track_ids(456)

    assert len(ids) == 2500
    assert ids == all_ids


@pytest.mark.asyncio
@respx.mock
async def test_song_details_parse() -> None:
    """验收 3：song/detail 解析 name/ar/al/alia/originSongSimpleData。"""
    song = {
        "id": 1001,
        "name": "夜曲",
        "ar": [{"id": 1, "name": "周杰伦"}, {"id": 2, "name": "合唱者"}],
        "al": {"id": 10, "name": "十一月的萧邦"},
        "alia": ["夜曲（Live）", "夜曲伴奏"],
        "originSongSimpleData": {"id": 999, "name": "原曲名", "artists": [{"name": "原唱"}]},
    }
    respx.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": [song]})
    )

    client = NcmClient(httpx.AsyncClient())
    async with client:
        result = await client.fetch_song_details([1001])

    assert len(result) == 1
    item = result[0]
    assert item["ncm_id"] == 1001
    assert item["name"] == "夜曲"
    assert item["artist"] == "周杰伦/合唱者"
    assert item["album"] == "十一月的萧邦"
    assert item["alia"] == ["夜曲（Live）", "夜曲伴奏"]  # JSON 数组原样保留
    assert item["origin"] == {"id": 999, "name": "原曲名", "artists": [{"name": "原唱"}]}


@pytest.mark.asyncio
@respx.mock
async def test_song_details_batch_size_1000() -> None:
    """song/detail 每批 ≤1000：2500 个 id → 恰好 3 次 POST，body c 正确。"""
    respx.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": []})
    )

    client = NcmClient(httpx.AsyncClient())
    async with client:
        await client.fetch_song_details(list(range(1, 2501)))

    assert len(respx.calls) == 3
    # 每批 id 数量 ≤1000 且互不重叠
    batch_ids: list[list[int]] = []
    for call in respx.calls:
        body = call.request.content.decode()
        assert body.startswith("c=")
        ids = [item["id"] for item in json.loads(unquote_plus(body[2:]))]
        assert 1 <= len(ids) <= 1000
        batch_ids.append(ids)
    all_sent = [i for batch in batch_ids for i in batch]
    assert all_sent == list(range(1, 2501))


@pytest.mark.asyncio
@respx.mock
async def test_retry_twice_then_raise() -> None:
    """验收 4：请求失败重试 2 次后抛出异常。"""
    respx.get(PLAYLIST_URL).mock(side_effect=httpx.ConnectError("network down"))

    client = NcmClient(httpx.AsyncClient(), retries=2)
    async with client:
        with pytest.raises(NcmError):
            await client.fetch_playlist_track_ids(123)

    # 初始 1 次 + 重试 2 次 = 3 次尝试
    assert len(respx.calls) == 3


@pytest.mark.asyncio
@respx.mock
async def test_http_500_retry_then_raise() -> None:
    """HTTP 500 属于失败，重试 2 次后抛出。"""
    respx.get(PLAYLIST_URL).mock(return_value=httpx.Response(500, json={}))

    client = NcmClient(httpx.AsyncClient(), retries=2)
    async with client:
        with pytest.raises(NcmError):
            await client.fetch_playlist_track_ids(123)
    assert len(respx.calls) == 3


@pytest.mark.asyncio
@respx.mock
async def test_wiki_method_available() -> None:
    """fetch_song_wiki 方法可用并返回 data（本步无调用方，仅保证可用）。"""
    respx.get(WIKI_URL, params={"id": "777"}).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"summary": "这是一首歌的百科"}}
        )
    )
    client = NcmClient(httpx.AsyncClient())
    async with client:
        result = await client.fetch_song_wiki(777)
    assert result == {"summary": "这是一首歌的百科"}
