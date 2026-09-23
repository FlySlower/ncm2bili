"""报告生成（里程碑 M4）：preview_report.html + report.csv。

preview_report.html 逐首展示 song_key、最终 BV、method、评分明细（score_detail
展开为候选对比列表）；report.csv 为结构化数据（utf-8-sig 兼容 Excel）。
"""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path

from db import Database

_METHOD_CLASS = {
    "WHITELIST_BV": "whitelist",
    "UPLOADER_WL": "uploader",
    "SCORED": "scored",
    "MANUAL": "manual",
}
_METHOD_LABEL = {
    "WHITELIST_BV": "白名单",
    "UPLOADER_WL": "UP主白名单",
    "SCORED": "评分",
    "MANUAL": "人工",
}

_HTML_TPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>ncm2bili 预览报告（dry-run）</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; }}
.summary {{ color: #555; font-size: 13px; margin: 8px 0 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 13px;
         text-align: left; vertical-align: top; }}
th {{ background: #f5f5f5; }}
.badge {{ padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 12px; }}
.whitelist {{ background: #0a7d32; }}
.uploader {{ background: #0969da; }}
.scored {{ background: #7d4a09; }}
.manual {{ background: #c0392b; }}
.candidates {{ margin: 0; padding-left: 18px; }}
.score-detail {{ color: #333; }}
</style>
</head>
<body>
<h1>ncm2bili 预览报告（dry-run）</h1>
<div class="summary">生成时间：{generated} ｜ 共 {total} 首 ｜ {method_summary}</div>
<table>
<thead>
<tr><th>#</th><th>song_key</th><th>歌曲</th><th>歌手</th><th>method</th>
<th>最终 BV</th><th>评分明细（候选对比）</th><th>失败原因</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""

_ROW_TPL = """<tr>
<td>{idx}</td>
<td>{song_key}</td>
<td>{name}</td>
<td>{artist}</td>
<td><span class="badge {method_class}">{method_label}</span></td>
<td>{bvid}</td>
<td class="score-detail">{score_detail}</td>
<td>{fail_reason}</td>
</tr>"""


def load_report_rows(db: Database, task_id: str | None = None) -> list[dict]:
    """从 songs 表读取报告数据；task_id 指定时仅返回该任务的歌曲（默认全部任务）。"""
    if task_id:
        return [
            dict(row)
            for row in db.query(
                "SELECT * FROM songs WHERE task_id = ? ORDER BY song_key", (task_id,)
            )
        ]
    return [dict(row) for row in db.query("SELECT * FROM songs ORDER BY song_key")]


def write_csv(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["song_key", "name", "artist", "status", "method", "bvid",
             "score_detail", "fail_reason", "updated_at"]
        )
        for r in rows:
            writer.writerow(
                [r["song_key"], r["name"], r["artist"], r["status"], r["method"],
                 r["bvid"], r["score_detail"], r["fail_reason"], r["updated_at"]]
            )
    return path


def _render_score_detail(score_detail: str | None) -> str:
    """把 score_detail JSON 展开为可读 HTML（关键词 + 候选对比列表）。"""
    if not score_detail:
        return ""
    try:
        data = json.loads(score_detail)
    except json.JSONDecodeError:
        return html.escape(str(score_detail))
    parts = [f"关键词：{html.escape(str(data.get('keyword', '')))}"]
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts.append('<ul class="candidates">')
        for i, cand in enumerate(candidates, 1):
            title = html.escape(str(cand.get("title", "")))
            bvid = html.escape(str(cand.get("bvid", "")))
            score = cand.get("score", "")
            parts.append(f"<li>#{i} {title}（bvid={bvid}，score={score}）</li>")
        parts.append("</ul>")
    return "".join(parts)


def write_preview_html(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    method_counts: dict[str, int] = {}
    body_parts: list[str] = []
    for idx, row in enumerate(rows, 1):
        method = row["method"] or "MANUAL"
        method_counts[method] = method_counts.get(method, 0) + 1
        body_parts.append(
            _ROW_TPL.format(
                idx=idx,
                song_key=html.escape(row["song_key"] or ""),
                name=html.escape(row["name"] or ""),
                artist=html.escape(row["artist"] or ""),
                method_class=_METHOD_CLASS.get(method, "manual"),
                method_label=_METHOD_LABEL.get(method, method),
                bvid=html.escape(row["bvid"] or ""),
                score_detail=_render_score_detail(row["score_detail"]),
                fail_reason=html.escape(row["fail_reason"] or ""),
            )
        )
    method_summary = " ｜ ".join(
        f"{_METHOD_LABEL.get(m, m)} {n} 首" for m, n in sorted(method_counts.items())
    )
    page = _HTML_TPL.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total=len(rows),
        method_summary=html.escape(method_summary),
        rows="\n".join(body_parts),
    )
    path.write_text(page, encoding="utf-8")
    return path


def write_reports(
    db: Database,
    output_dir: str | Path = "output",
    task_id: str | None = None,
) -> tuple[Path, Path]:
    """一次性生成 preview_report.html 与 report.csv，返回两个文件路径。

    task_id 指定时仅报告该任务的歌曲（文档 §3 阶段三"报告（绑定 task_id）"）。
    """
    rows = load_report_rows(db, task_id=task_id)
    output_dir = Path(output_dir)
    csv_path = write_csv(rows, output_dir / "report.csv")
    html_path = write_preview_html(rows, output_dir / "preview_report.html")
    return csv_path, html_path


# ---- 人工回灌 review.html（文档 §10.2/§10.3）-------------------------

_REVIEW_HTML_TPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>ncm2bili 人工回灌（review）</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; }}
.summary {{ color: #555; font-size: 13px; margin: 8px 0 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 13px;
         text-align: left; vertical-align: middle; }}
th {{ background: #f5f5f5; }}
.badge {{ padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 12px;
         background: #c0392b; }}
input {{ width: 200px; padding: 3px 6px; font-size: 13px; }}
button {{ padding: 3px 12px; font-size: 13px; cursor: pointer; }}
.status {{ margin-left: 8px; font-size: 12px; }}
.status.ok {{ color: #0a7d32; }}
.status.err {{ color: #c0392b; }}
</style>
</head>
<body>
<h1>ncm2bili 人工回灌（review）</h1>
<div class="summary">共 {count} 首待人工回灌 ｜ 搜索后填入 BV 号点击保存 ｜ 保存服务：{server_hint}</div>
<table>
<thead>
<tr><th>#</th><th>song_key</th><th>歌曲</th><th>歌手</th><th>搜索</th>
<th>BV 号</th><th>操作</th><th>失败原因</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<script>
const SAVE_URL = "{save_url}";
const TOKEN = "{token}";
async function saveManual(btn) {{
  const tr = btn.closest("tr");
  const input = tr.querySelector("input");
  const status = tr.querySelector(".status");
  const bvid = input.value.trim();
  if (!/^BV1[a-zA-Z0-9]{{9}}$/.test(bvid)) {{
    status.textContent = "BV 号格式不正确（应为 BV1 开头共 12 位）";
    status.className = "status err";
    return;
  }}
  try {{
    const resp = await fetch(SAVE_URL, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json", "X-Token": TOKEN }},
      body: JSON.stringify({{ song_key: btn.dataset.songKey, bvid: bvid }}),
    }});
    const data = await resp.json();
    if (resp.ok) {{
      status.textContent = "已保存 ✓";
      status.className = "status ok";
    }} else {{
      status.textContent = "保存失败：" + (data.error || resp.status);
      status.className = "status err";
    }}
  }} catch (e) {{
    status.textContent = "无法连接保存服务（本地服务未启动？）";
    status.className = "status err";
  }}
}}
// F1-4（§10.3）：事件委托，点击保存按钮触发（废除 onclick 拼接）
document.addEventListener("click", (e) => {{
  const btn = e.target.closest("button[data-song-key]");
  if (btn) saveManual(btn);
}});
</script>
</body>
</html>
"""

_REVIEW_ROW_TPL = """<tr>
<td>{idx}</td>
<td>{song_key}</td>
<td>{name}</td>
<td>{artist}</td>
<td><a href="{search_url}" target="_blank" rel="noopener">B 站搜索</a></td>
<td><input placeholder="BV1xxxxxxxxx"></td>
<td><button data-song-key="{song_key_attr}">保存</button>
<span class="status"></span></td>
<td>{fail_reason}</td>
</tr>"""


def write_review_html(
    rows: list[dict],
    path: str | Path,
    *,
    save_url: str | None = None,
    token: str = "",
    search_base: str = "https://search.bilibili.com/all?keyword={kw}",
) -> Path:
    """生成人工回灌页 review.html（MANUAL / FAV_FAILED 歌曲，不内嵌任何 cookie/凭证）。

    F1-4（§10.3）：song_key 经 data-* 属性 + 事件委托传递，废除 onclick 拼接；
    token 注入页面 JS 供 fetch 携带 X-Token 头。

    Args:
        rows: songs 表行；仅 status=MANUAL / FAV_FAILED（或 method=MANUAL）的歌曲进入列表，
              FAV_FAILED 行内展示收藏失败原因（文档 §10.2）。
        save_url: 保存服务地址（如 http://127.0.0.1:8080/save）；
                  None 时页面提示"服务未启动"。
        token: 一次性 token（F1-4 §10.3），由 start_review_server 返回；空串时页面
               仍生成但 fetch 不带 X-Token（服务端会 403）。
        search_base: B 站搜索跳转链接模板，{kw} 会被替换为 URL 编码关键词。
    """
    from urllib.parse import quote

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    manual_rows = [
        r for r in rows if (r.get("status") or "MANUAL") in ("MANUAL", "FAV_FAILED")
        or (r.get("method") or "MANUAL") == "MANUAL"
    ]

    body_parts: list[str] = []
    for idx, row in enumerate(manual_rows, 1):
        name = row.get("name") or ""
        artist = row.get("artist") or ""
        keyword = f"{name} {artist}".strip()
        song_key = row.get("song_key") or f"{name}|{artist}"
        # F1-4（§10.3）：data-* 属性传参，html.escape 转义属性值（含引号）
        body_parts.append(
            _REVIEW_ROW_TPL.format(
                idx=idx,
                song_key=html.escape(song_key),
                name=html.escape(name),
                artist=html.escape(artist),
                search_url=search_base.format(kw=quote(keyword)),
                song_key_attr=html.escape(song_key, quote=True),
                fail_reason=html.escape(row.get("fail_reason") or ""),
            )
        )

    if save_url:
        server_hint = "已启动" if "://" in save_url else save_url
        safe_url = html.escape(save_url)
    else:
        server_hint = "未启动（本页保存不可用）"
        safe_url = ""
    safe_token = html.escape(token)

    page = _REVIEW_HTML_TPL.format(
        count=len(manual_rows),
        server_hint=html.escape(server_hint),
        save_url=safe_url,
        token=safe_token,
        rows="\n".join(body_parts),
    )
    path.write_text(page, encoding="utf-8")
    return path


__all__ = [
    "load_report_rows",
    "write_csv",
    "write_preview_html",
    "write_reports",
    "write_review_html",
]
