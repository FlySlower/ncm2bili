# 网易云歌单 → B站收藏夹 转换工具 · 技术文档

| 项目 | 内容 |
| :--- | :--- |
| **版本** | v0.3（v0.1 + v0.2 合并修订版） |
| **日期** | 2026-09-21 |
| **技术栈** | Python 3.11+ / httpx / asyncio / SQLite / Pydantic / playwright(仅 auth) |
| **目标规模** | 单歌单最大 3000 首，全程 ≤ 20 分钟 |

---

## 0. 变更记录

### v0.1 → v0.2 变更（整理自两份文档，原 v0.2 未记录，此处补记）

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 凭证获取改为 `python main.py auth` 自动捕获 | **架构级** | v0.1 为手动从浏览器复制 cookie.txt；v0.2 起引入 playwright 自动捕获，新增依赖 |
| 新增 `--dry-run` 模式 | 功能 | 匹配全流程跑完但不调用写接口，生成 preview_report.html |
| 搜索/收藏间隔增加随机抖动 (jitter) | 改进 | 固定间隔易形成请求规律，jitter 更贴近真实用户 |
| ~~每请求随机 UA~~ → **会话级固定 UA** | v0.3 修正 | v0.2 的随机 UA 与登录态混用本身就是风控特征，v0.3 撤销 |
| review.html 支持页面内直接填 BV 号保存 | 功能 | 替代手动编辑 manual.json |
| 新增 §11 安装部署、§12 日志规范、§13 测试策略 | 工程化 | — |
| **静默丢失 v0.1 内容**（§2 三条约束、§3.1 目录结构、§5 接口细节、§6 DDL、§8 推导过程） | **缺陷** | v0.3 全部恢复并合并进正文 |
| **引入优先级矛盾**（§4.1 与 §10.2 不一致） | **缺陷** | v0.3 统一为 `manual > whitelist > uploaders > 评分` |
| **引入双套风控策略**（固定暂停 vs 指数退避未定义组合方式） | **缺陷** | v0.3 统一：请求级指数退避 + 全局固定暂停，职责分离 |

### v0.2 → v0.3 变更

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 全文合并 v0.1 被丢失的章节，删除"与 v0.1 保持一致"类自引用 | 修正 | 文档独立可读 |
| 统一优先级铁律为 `manual.json > whitelist.json > uploaders.json > 评分`，全文唯一出处 §4.1 | 修正 | — |
| 风控策略分层定义：单请求指数退避 / 全局固定暂停 + 降并发 / 间隔 jitter | 修正 | 三层职责见 §7 |
| 随机 UA 改为 session 级固定 UA + 完整浏览器 header 集合 | 修正 | — |
| 补充 auth 实现方案与 cookie rotate 处理（§11.3） | 补充 | — |
| 补充 review.html 本地保存服务设计（§10.3） | 补充 | — |
| 收藏幂等：区分"已收藏"与真正失败（§9.3） | 补充 | 重跑不再误报 |
| 日志 Cookie 脱敏规则（§12） | 补充 | — |
| 收藏夹拆分与命名策略（§5.3） | 补充 | — |
| 测试：移除 requests-mock（项目用 httpx），统一 respx | 修正 | — |

---

## 1. 项目概述

输入一个网易云音乐歌单（≤3000 首），为每首歌在 B 站匹配最合适的视频，并批量添加到 B 站收藏夹。

### 1.1 设计目标

- **正确性优先**：通过多层筛选机制（白名单 / 黑名单 / 两阶段评分 / 降级 / 人工回灌）保证匹配质量。
- **速度可接受**：3000 首全流程 ≤ 20 分钟（瓶颈在 B 站接口响应速度，而非本机算力）。
- **可断点续跑**：任何时刻中断，重启后从断点继续，已完成的工作不重复。
- **可人工介入**：自动解决不了的歌进入人工队列，人工结果可回灌并被最高优先级采用。

### 1.2 非目标

- 不做音频下载 / 转存（纯收藏夹管理）。
- 不做分布式（单机 asyncio 并发即可达标；预留替换调度层的可能，见 §14）。
- 不做完整的 Web 界面（CLI + review.html + 本地保存服务足够，见 §10.3）。

---

## 2. 关键外部约束（设计前提）

| 约束 | 影响 |
| :--- | :--- |
| B 站单个收藏夹上限 **1000 个视频**，收藏夹总数上限约 **99 个** | 3000 首必须自动拆分为多个收藏夹；按歌手/主题建夹等功能受 99 上限约束 |
| B 站搜索接口已强制 **WBI 签名**（2025-05 起），且要求 `buvid3` cookie（2025-06 起） | 搜索模块必须实现 WBI 签名与 buvid3 预取 |
| 收藏接口 `/x/v3/fav/resource/add` 需要登录态（`SESSDATA` + `bili_jct`） | 用户必须授权；程序通过 `auth` 命令自动捕获并安全存储凭证 |
| B 站有风控（HTTP 412 / body code `-412`），高频、规律化请求会触发 | 间隔 jitter + 并发上限 + 分层退避，不可绕过只能尊重 |
| 网易云 `playlist/detail` 返回的 `tracks` 不完整，完整曲目在 `trackIds` | 需二次调用 `song/detail` 批量取详情 |
| 网易云 `song/wiki/summary` **每首歌单独一次请求，无批量接口** | 作为第 4 轮降级"救场"步骤仅对失败歌曲触发，控制调用量 |

---

## 3. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│ 配置文件层  whitelist.json / uploaders.json / blacklist_words.json │
│            manual.json / config.yaml / credentials.db        │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段零：授权（一次性）                                          │
│   python main.py auth ──► playwright 打开 B 站 ──► 登录 ──►    │
│   捕获 Cookie ──► 加密存入 credentials.db                     │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段一：歌单抓取（网易云，秒级）                                │
│   playlist/detail ──► trackIds（分页，≤1000/页）               │
│   song/detail     ──► 歌名/歌手/专辑/alia(别名)/原曲信息        │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段二：逐首匹配（核心，asyncio 并发 4）                        │
│   每首歌的状态机闯关：                                         │
│   WHITELIST_BV ─► SEARCH ─► BLACKLIST ─► UPLOADER_WL        │
│   ─► SCORING(两阶段) ─► RETRY(降级关键词) ─► WIKI ─► MANUAL   │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段三：批量收藏（B站，并发 2~3）                               │
│   自动建收藏夹（每 1000 首一个）──► 逐条 add ──► 报告           │
│   （--dry-run 时跳过本阶段，改为 preview_report.html）         │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段四：人工回灌                                               │
│   review.html ──► 页面内填 BV 保存（本地服务）──► manual.json  │
│   ──► 重跑（manual 优先级最高）──► DONE                        │
└─────────────────────────────────────────────────────────────┘

横切：SQLite（缓存+状态机）│ 分层风控（§7）│ 结构化日志（§12）│ 测试（§13）
```

### 3.1 目录结构

```
ncm2bili/
├── main.py                  # CLI 入口（auth / run / report），编排各阶段
├── config.py                # 限速/并发/权重等可调参数（可由 config.yaml 覆盖）
├── ncm.py                   # 网易云模块（歌单/详情/百科）
├── bili_search.py           # B站搜索 + WBI 签名 + buvid3 预取
├── scorer.py                # 两阶段评分
├── matcher.py               # 状态机闯关逻辑（阶段二）
├── fav.py                   # 收藏夹创建与批量收藏（阶段三）
├── report.py                # CSV 报告 / review.html / preview_report.html（阶段四）
├── review_server.py         # review.html 的本地保存服务（localhost only，§10.3）
├── auth.py                  # playwright 授权与凭证加密存储（阶段零）
├── db.py                    # SQLite 封装
├── whitelist.json           # {"歌名|歌手": "BV号"}
├── uploaders.json           # {"mid": {"name": "...", "note": "..."}}
├── blacklist_words.json     # ["串烧", "精选", ...]
├── manual.json              # 人工回灌 {"歌名|歌手": "BV号"}
├── config.yaml              # 并发/间隔/超时等配置
├── credentials.db           # 加密存储的 B 站 cookie（权限 600）
└── cache.db                 # SQLite 缓存与状态
```

---

## 4. 核心机制设计

### 4.1 每首歌的状态机

```
        ┌──────────────┐
        │   PENDING    │
        └──────┬───────┘
   ①whitelist.json 精确命中?         是 ──► DONE (method=WHITELIST_BV)
               │ 否
               ▼
   ②B站搜索 "歌名 歌手"
               │
               ▼
   ③黑名单过滤（标题归一化后含黑名单词 → 淘汰并记因）
               │
               ▼
   ④uploaders.json 命中 mid 且标题含歌名?   是 ──► DONE (method=UPLOADER_WL)
               │ 否
               ▼
   ⑤两阶段评分 ──► 有合格候选?      是 ──► DONE (method=SCORED)
               │ 否
               ▼
   ⑥降级关键词重试（歌名 / 歌名+现场），≤2 轮 ──► 成功则回 ③
               │ 仍失败
               ▼
   ⑦/wiki 救场：用 alia、originSongSimpleData、wiki 摘要拼新关键词 ──► 回 ③
               │ 仍失败
               ▼
           MANUAL（method=MANUAL 待人工）──► review.html 回灌 ──► DONE
```

**优先级铁律（全文唯一出处）**：

```
manual.json（人工） > whitelist.json（精确BV） > uploaders.json（账号） > 评分
```

- 人工回灌结果一旦写入 `manual.json` 并重跑，**永远覆盖**自动结果——这是"人工可介入"设计目标的落点。
- 状态机每次启动时先做优先级重查：一首歌即使已 DONE，若 `manual.json` 后来新增了对应 BV，重跑时应升级为人工结果。
- 黑名单过滤在账号白名单**之前**执行——白名单免打分，不免内容审查。

### 4.2 两阶段评分

**阶段一（零额外请求，仅用搜索返回字段）**：

```
score = w1·log10(播放量+1)
      + w2·log10(收藏量+1)
      + w3·log10(评论量+1)
      + title_bonus        # 标题含歌名 +10；含"官方/原唱/MV/音频/歌词/完整版" 每项 +3
      + duration_bonus     # 1~8 分钟内 +5；<30s 或 >10min −10
      − author_penalty     # 粉丝极少但播放异常高的营销号特征 −15
```

取前 3 名进入阶段二。

**阶段二（补调详情，每首歌 ≤3 次额外请求）**：

- `/x/web-interface/view` → 赞 / 币 / 收藏 细分
- `/x/relation/stat` → UP 主粉丝数（**按 mid 缓存，同一 UP 主全程序只查一次**）

阶段二仅用于差值接近时精排；差值明显时直接用阶段一结果，省下请求。

### 4.3 关键词降级链

| 轮次 | 关键词 | 说明 |
| :--- | :--- | :--- |
| 1 | `歌名 歌手` | 默认 |
| 2 | `歌名` | 歌手可能有误或生僻 |
| 3 | `歌名 现场` / `歌名 音频` | 视情况二选一 |
| 4（救场） | `别名 歌手` / `歌名 原唱歌手` / `中文译名 歌手` | 来自 alia / originSongSimpleData / wiki；注意 wiki 接口无批量，每首歌一次请求，仅走到此轮才调用 |

### 4.4 断点续跑

- 每首歌的**每个状态跃迁立即落盘** SQLite（§6 的 `songs` 表）。
- 搜索缓存以 `歌名|歌手` 为 key：调整评分权重后重跑，只重打分、不重搜索。
- 已 DONE 的歌永不再发请求——除非：
  - 用户显式传 `--refresh`（丢弃匹配结果，保留歌单数据）；
  - 或 `manual.json` 新增了优先级更高的结果（见 §4.1）。
- **增量语义**：歌单新增歌曲时，旧歌曲命中缓存直接 DONE，只有新歌走完整状态机。

---

## 5. 外部接口清单

### 5.1 网易云（无需登录）

| 用途 | 接口 | 备注 |
| :--- | :--- | :--- |
| 歌单曲目 | `GET music.163.com/api/v6/playlist/detail?id=&n=1000&offset=` | `trackIds` 完整；分页至 3000 |
| 歌曲详情 | `POST music.163.com/api/v3/song/detail`，body `c=[{id:..},...]` | 每批 ≤1000 |
| 音乐百科 | `GET music.163.com/api/song/wiki/summary?id=` | 救场专用，失败歌曲才调；无批量接口 |

从 `song/detail` 免费获得：`name`、`ar`（歌手）、`al`（专辑）、**`alia`（别名）**、**`originSongSimpleData`（翻唱的原曲信息）**。

### 5.2 B 站（需 cookie）

| 用途 | 接口 | 认证 | 备注 |
| :--- | :--- | :--- | :--- |
| WBI key | `GET api.bilibili.com/x/web-interface/nav` | 可选 | 取 img_key/sub_key |
| 搜索视频 | `GET api.bilibili.com/x/web-interface/wbi/search/type?search_type=video&keyword=&page=` | buvid3 | **必须 WBI 签名** |
| 视频详情 | `GET api.bilibili.com/x/web-interface/view?bvid=` | 可选 | 三连细分（阶段二） |
| UP主信息 | `GET api.bilibili.com/x/relation/stat?vmid=` | 可选 | 粉丝数，按 mid 缓存 |
| 创建收藏夹 | `POST api.bilibili.com/x/v3/fav/folder/add` | SESSDATA+bili_jct | body 含 csrf |
| 收藏 | `POST api.bilibili.com/x/v3/fav/resource/add` | SESSDATA+bili_jct | body: `rid&type=2&media_id&csrf` |

**错误码约定**：B 站错误同时体现在 HTTP 状态码与 body 的 `code` 字段。本文统一以 **API code**（body 中的值）为准；412 指 HTTP 状态码，`-412` 指 API code，两者同义均按风控处理。

**WBI 签名算法**：

1. `nav` 取 `img_key + sub_key`；
2. 拼接后按固定表 `MIXIN_KEY_ENC_TAB` 重排，截取 32 位得 `mixin_key`；
3. 参数加 `wts`（当前时间戳）后按字典序拼接 + `mixin_key` 做 MD5 得 `w_rid`；
4. 参数值中 `!'()*` 需转义。

`img_key/sub_key` 缓存于内存 + 落盘 `cache.db`，TTL 约 1 天；遇到 `-403`（签名/时间戳错误）时强制刷新重取一次。

**buvid3 获取**：启动时先访问 `bilibili.com` 主页，从 Set-Cookie 抓取，存入凭证表随请求携带。

### 5.3 收藏夹拆分策略

- 目标收藏夹上限按 **900/个** 留余量（防并发写入时超限），3000 首拆为 ⌈N/900⌉ 个。
- 命名：`<歌单名> (1)`、`(2)`……，重跑时优先复用已存在的同名夹（按名称查询，取其 `media_id`），不为同一任务重复建夹。
- 建夹失败（如达 99 上限，API code `-400`）→ 停止收藏阶段，输出明确错误与当前进度，支持修复后断点续跑。

---

## 6. 数据模型（SQLite）

```sql
CREATE TABLE songs (
  song_key    TEXT PRIMARY KEY,   -- "歌名|歌手"
  ncm_id      INTEGER,
  name        TEXT,
  artist      TEXT,
  album       TEXT,
  alia        TEXT,               -- JSON 数组
  origin      TEXT,               -- 翻唱原曲信息 JSON
  status      TEXT,               -- PENDING/DONE/MANUAL
  method      TEXT,               -- MANUAL/WHITELIST_BV/UPLOADER_WL/SCORED
  bvid        TEXT,
  score_detail TEXT,              -- 评分明细 JSON（抽查调权重用）
  fail_reason TEXT,
  updated_at  INTEGER
);

CREATE TABLE search_cache (
  keyword    TEXT PRIMARY KEY,
  results    TEXT,                -- 候选 JSON
  fetched_at INTEGER
);

CREATE TABLE uploader_cache (
  mid       INTEGER PRIMARY KEY,
  followers INTEGER,
  fetched_at INTEGER
);

CREATE TABLE whitelist_bv (
  song_key TEXT PRIMARY KEY,
  bvid     TEXT,
  source   TEXT                   -- whitelist / manual
);

CREATE TABLE credentials (
  key       TEXT PRIMARY KEY,     -- 'bili_cookie'
  value     TEXT,                 -- Fernet 加密后的 cookie 串
  updated_at INTEGER
);

CREATE TABLE kv_meta (
  key   TEXT PRIMARY KEY,         -- 如 wbi_keys
  value TEXT,
  fetched_at INTEGER
);
```

---

## 7. 并发与风控策略（三层，职责分离）

| 层级 | 机制 | 参数（config.yaml 可调） | 职责 |
| :--- | :--- | :--- | :--- |
| **预防层** | 间隔抖动：每次请求 sleep = 基础间隔 + uniform(jitter) | 搜索：400ms + 100~300ms；收藏：500ms + 100~200ms | 避免请求时间间隔规律化 |
| **身份层** | 会话级固定 UA + 完整 header 集合 | UA 从 3 个真实浏览器 UA 中**启动时随机选一个并全程固定**；补齐 sec-ch-ua / Accept-Language / Referer | 模拟真实浏览器；**严禁**在同一 session 内切换 UA（与 SESSDATA 混用是风控特征） |
| **响应层 a** | 单请求指数退避：412/网络错误时该请求重试，间隔 2s→4s→8s，最多 3 次 | 初始 2s，倍数 2，上限 3 次 | 处理瞬时抖动 |
| **响应层 b** | 全局熔断：滑动窗口 60s 内 API code `-412` 达 3 次 → 全局暂停 60s；达 5 次 → 暂停 5min 并降并发 50%、输出 WARNING | 窗口 60s；阈值 3/5 | 持续风控时主动冷却 |
| **响应层 c** | 认证失效：API code `-101` / `-111` → 立即中止，提示 `python main.py auth` 重新授权 | — | 凭证类错误不重试 |

收藏写操作沿用更保守参数；所有限速参数集中在 `config.yaml`，可随时调。

---

## 8. 性能估算（3000 首）

| 阶段 | 请求量 | 计算 | 预估耗时 |
| :--- | :--- | :--- | :--- |
| 网易云抓取 | ~10 | 分页 3 次 + song/detail 3 批 | <10s |
| 搜索匹配 | ~3000 × 1.1（重试/救场 10%） | 3300 × (0.4s + 0.2s均值抖动) ÷ 并发4 ≈ 495s | 6~10 min |
| 阶段二补查 | ≤3000（差值明显时跳过，预估实际命中 30%） | 900 × 0.5s ÷ 4 ≈ 113s | 含在上项 |
| 收藏 | 3000 | 3000 × (0.5s + 0.15s) ÷ 并发2.5 ≈ 780s | 5~8 min |
| 风控退避开销 | — | 按 5% 请求触发一次 60s 暂停估算 | +1~2 min |
| **合计** | | | **12~20 min** |

---

## 9. 错误处理

### 9.1 风控（412 / -412）

见 §7 响应层：单请求指数退避（a）处理瞬时抖动，全局熔断（b）处理持续风控。412 **不视为单首歌失败**，重试耗尽才降级记录。

### 9.2 认证失效（-101 / -111）

立即中止当前阶段，提示重新执行 `python main.py auth`。此类错误不做退避重试。

### 9.3 收藏幂等

`/x/v3/fav/resource/add` 对"已在夹中"通常返回特定 code（如 `-404` 之外的重复提示，以实测为准）。程序必须区分：

- **已收藏**：视为成功，状态置 DONE，不记 fail_reason；
- **视频不存在/被删（-404 等）**：状态回 MANUAL 并注明，进入人工队列；
- **其他失败**：记 `fail_reason` 后继续下一首，单首歌永不使整体任务失败。

### 9.4 凭证安全

- cookie 经 Fernet 对称加密后存入 `credentials.db`（文件权限 600），密钥派生自机器特征（如 `/etc/machine-id`）或首次运行时随机生成并存放于用户目录 600 权限文件；
- **任何日志、报告、异常堆栈不得输出完整 cookie**：`SESSDATA`、`bili_jct` 一律脱敏为前 4 位 + `***`（见 §12）；
- 文档建议用户用小号测试。

---

## 10. 人工回灌与 Dry-Run

### 10.1 Dry-Run 模式

- `python main.py run <歌单ID> --dry-run`：执行阶段一、二、四报告，**跳过阶段三**。
- 生成 `output/preview_report.html`，逐首展示：最终候选 BV、命中方式（method）、评分明细、各候选对比。用户确认后再正式运行。
- dry-run 不写 `manual.json`、不建收藏夹，可安全重复执行。

### 10.2 人工回灌流程

1. 正式跑完后 `output/review.html` 列出所有 MANUAL 歌曲，每首附 B 站搜索跳转链接。
2. 用户在 review.html 页面内直接填写 BV 号并保存（见 §10.3），或手动编辑 `manual.json`（`{"歌名|歌手": "BV1xxxxx"}`）——两种方式等价。
3. 重跑程序：`manual.json` 按 §4.1 优先级铁律**最高优先级**生效，直接入夹。

### 10.3 review.html 本地保存服务

- `report.py` 生成 review.html 后，可选择启动 `review_server.py`：`127.0.0.1` 随机端口，仅监听回环地址，仅处理 POST `/save`。
- 页面内嵌 fetch 调用 `http://127.0.0.1:<port>/save`，body 为 `{song_key, bvid}`；服务端做 BV 号格式校验（`^BV1[a-zA-Z0-9]{9}$`）。
- 写入采用"读-改-写 + 原子替换"（写临时文件后 `os.replace`），防止并发写损坏 `manual.json`。
- 服务生命周期 = 用户浏览器标签页打开期间；用户也可完全跳过它走手动编辑路径。该服务不暴露任何歌单/cookie 数据，仅接收 BV 号。
- 此服务为**本地开发便利设施**，非 Web 产品（非目标 §1.2 仍然成立）。

---

## 11. 配置与部署

### 11.1 环境要求

- Python ≥ 3.11（使用 `asyncio.TaskGroup` 与 `tomllib`/类型标注新语法）。

### 11.2 安装步骤

```bash
# 1. 克隆项目
git clone https://github.com/your-username/ncm2bili.git
cd ncm2bili

# 2. 创建并激活虚拟环境
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux

# 3. 安装依赖
pip install -r requirements.txt   # 含 httpx, pydantic, playwright, cryptography, pytest, respx
playwright install chromium       # auth 功能需要
```

### 11.3 授权（阶段零）

1. 首次运行 `python main.py auth`。
2. playwright 打开 Chromium 跳转 B 站首页，用户完成登录。
3. 程序监听 cookie 变化，检测到 `SESSDATA + bili_jct + buvid3` 齐备后自动抓取，加密写入 `credentials.db` 并关闭浏览器。
4. **cookie rotate 处理**：B 站会不定期刷新 cookie。程序在运行中收到 `-101`/`-111` 时提示重新 auth（§9.2）；auth 重复执行视为刷新，覆盖旧凭证。
5. 无桌面环境（服务器）时，auth 命令提供 `--paste` 回退：提示用户从浏览器手动复制 cookie 字符串粘贴（等效 v0.1 行为）。
6. 并发/间隔/权重等参数在 `config.yaml` 中修改，均有默认值。

### 11.4 运行程序

```bash
# 1. 模拟运行（推荐首次使用）
python main.py run <网易云歌单ID> --dry-run
# 检查 output/preview_report.html，确认匹配质量

# 2. 正式运行
python main.py run <网易云歌单ID>

# 3. 丢弃匹配结果重跑（保留歌单数据与缓存）
python main.py run <网易云歌单ID> --refresh
```

---

## 12. 日志与监控

- **模块**：Python 内置 `logging`。
- **级别约定**：

| 级别 | 用途示例 |
| :--- | :--- |
| INFO | 关键流程节点："开始匹配歌曲 X"、"收藏夹 XXX 创建成功"、"收藏成功 123/3000" |
| WARNING | 非致命问题："单首歌匹配失败"、"触发风控，全局暂停 60s"、"候选视频已失效，转入人工队列" |
| ERROR | 需人工介入："凭证失效，请重新 auth"、"收藏夹创建失败（已达 99 上限）" |
| DEBUG | 请求/响应明细，仅 `--debug` 时开启 |

- **输出**：
  - 控制台：INFO 及以上，格式简洁；
  - 文件：`logs/app_YYYY-MM-DD.log`，全级别，含时间/模块/级别。
- **脱敏铁律**：日志 Filter 在 Formatter 前对 `SESSDATA=[^;]*`、`bili_jct=[^;]*`、`buvid3=[^;]*` 统一替换为 `<redacted>`；DEBUG 级也不例外。测试用例覆盖该 Filter。

---

## 13. 测试策略

### 13.1 单元测试

- `scorer.py`：各评分项边界（播放量为 0、时长临界 30s/8min/10min、营销号特征组合）；
- `bili_search.py`：WBI 签名使用已知输入输出向量验证（社区公开测试向量）；
- `fav.py`：幂等分类逻辑（已收藏/视频不存在/其他失败三分支）；
- 日志脱敏 Filter。

### 13.2 集成测试

- 使用 `pytest + respx`（与 httpx 配套）模拟全部外部 API；
- 覆盖：歌单抓取 → 状态机闯关 → dry-run 报告 的完整链路；
- 异常注入：`-412` 连续触发（验证熔断）、`-101`（验证立即中止）、超时、收藏"已存在"响应；
- 断点续跑：中途杀掉进程，重启验证无重复请求。

### 13.3 Mock 铁律

- 所有外部网络请求必须被 Mock，CI 中禁止真实出站；
- 412/超时等异常路径必须被测到，而非只测 happy path。

---

## 14. 未来扩展路线

| 需求出现时才做 | 方案 |
| :--- | :--- |
| 多机分布式 | 调度层抽换为 Redis Streams；再不够上 RabbitMQ（durable queue + 死信交换机） |
| Web 界面 | review_server 升级为 FastAPI + 前端，人工回灌在线化 |
| 权重自学习 | 用 `score_detail` + 人工回灌结果做简单回归，自动调 w1/w2/w3 |

**当前明确不做**：RabbitMQ、分布式、公网 Web 后台。单机 SQLite + asyncio 已满足全部指标，避免过度设计。
