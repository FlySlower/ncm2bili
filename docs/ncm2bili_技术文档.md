# 网易云歌单 → B站收藏夹 转换工具 · 技术文档

| 项目 | 内容 |
| :--- | :--- |
| **版本** | v0.4（v0.1 + v0.2 合并修订版） |
| **日期** | 2026-09-22 |
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

### v0.3 → v0.3.1 变更

| 变更 | 说明 |
| :--- | :--- |
| v0.3 §5.2 的"buvid3 获取"一段替换为降级链设计 | — |

### v0.3.1 → v0.3.2 变更

| 变更 | 说明 |
| :--- | :--- |
| v0.3.1 §3 的数量与 §5.3 对齐 | — |
| 删除音乐 Wiki 相关信息 | — |

### v0.3.2 → v0.3.3 变更（2026-09-22，代码实测对齐）

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 收藏接口 `/x/v3/fav/resource/add` 废弃（404）→ 改用 `/x/v3/fav/resource/deal`，`rid` 为 av 号 | 修正 | 实测 2026-09-22；§2/§5.2/§9.3 |
| 新增 `-702`（"请求频率过高"）限流分类：与 `-412` 同级走熔断，退避基数 2 倍（4s→8s→16s） | 补充 | §7 响应层 b |
| 收藏默认保守参数：并发 1 / 1000ms + 200~400ms jitter（新账号防 -702） | 修正 | §7/§8 估算 |
| 降级链由 `config.yaml degrade.keywords` 驱动（`{name}`/`{artist}` 占位符），按文档 §4.3 | 修正 | §4.3/§3 状态机图 |
| 新增全局熔断器 `circuit_breaker.py` 与公共退避模块 `backoff.py` | 补充 | §7/§3.1 |
| 搜索缓存接通：`search_cache` 表按关键词命中，TTL 7 天（§4.4） | 补充 | §4.4 |
| 优先级统一入口 `whitelist_bv` 表（manual/whitelist 同步入库），内存 dict 移除 | 补充 | §4.1/§6 DDL |
| DONE 歌 manual 重查：启动时用更高优先级结果升级 | 补充 | §4.1 |
| --refresh 只清匹配、保留 search_cache | 补充 | §4.4/§11.4 |
| **任务制重构**：`run` 默认新建任务；新增 `task` 子命令（list/resume/delete）；`songs` 表加 `task_id`，新增 `tasks` 表；`matcher.py` 全链路带 task 作用域 | **架构级** | §1.1/§3/§4.4/§6/§11.4/§13 |

### v0.3.3 → v0.4 变更（2026-09-22，一致性收口）

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 降级链默认关键词 5 轮扩充至 9 轮（新增 4K/修复/完整版/歌词，移除舞台轮） | 改进 | §4.3，config 驱动；§8 估算系数 1.2→1.3 |
| 报告绑定 task_id：`load_report_rows`/`write_reports` 增加 task_id 参数，dry-run 报告只含当前任务 | 修复 | §3/§10.1；默认 None 保持全量（report --task-id 依赖） |
| 新增 `FAV_FAILED` 状态：收藏失败（非 -404/62002）置 FAV_FAILED，resume 只重跑阶段三（不重搜索），review.html 同 MANUAL 展示 | 功能 | §6/§4.4/§9.3/§10.2 |
| 收藏阶段接入全局熔断器：`-412`/`-702` 计入滑动窗口，达阈值全局暂停+降并发 | 修复 | §7 响应层 b（此前仅搜索阶段生效） |
| UA 随机选择落地：启动时从 `http.user_agents` 选一个并全程固定，传入搜索/收藏客户端 | 修复 | §7 身份层（此前为死配置，实际单一硬编码 UA） |
| 日志接入 CLI：入口调用 `setup_logging()`，新增 `--debug`，cookie 脱敏 Filter 生产路径生效 | 修复 | §12 |
| resume 路径注入 CircuitBreaker；修复 resume 摘要查询缺列导致的 IndexError | 修复 | §7/§11.4 |
| matcher/fav/bili_search 三处模块 docstring 对齐实现（WIKI 残留、旧降级链、旧收藏参数、旧 buvid3 主路径） | 修正 | §4.1/§4.3/§5.2/§7 |
| tasks 表 DDL 对齐代码（`finished_at`/`stats` 取代 `total_songs`/`completed_songs`，移除无写入路径的 CANCELLED） | 修正 | §6/§3/§11.4 |
| delete 语义统一：仅删 songs/tasks，保留 search_cache（§4.4/§13.2 原表述矛盾） | 修正 | §4.4/§13.2 |
| §11.4 补充 `report --task-id/--serve` 与 `task delete --all-finished`、`--yes` 参数文档 | 补充 | §11.4 |

### v0.4 → v0.4.1 变更

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 新增匹配置信度门槛 MatchGate：`match.min_score`/`match.min_margin` 双闸门（config 驱动），不达标置 MANUAL 并记 `LOW_CONFIDENCE` | 功能 | §4.1/§4.2；实测一 62 首错配率约 37% 驱动 |
| 新增关键词卫生 sanitize 管线：括号注释剥离、追加词仅出自降级链、remix 信息优先保留 | 修复 | §4.3；同上 |
| 新增 8 个实测一真实错配病例为 matcher 回归 fixture（Graveyard Phonk / Lake Arrowhead / in heat. / Vai Toma / Let Me Think About It / Constriction2.0 / 九万进行曲 / Sweet Sensation） | 测试 | §13.1 |
| 修复 search_cache 污染：根因为 B 站边缘缓存对相似查询返回陈旧响应（高亮词非查询词为特征），客户端读时归属校验（标题须含 sanitize 后歌名 token），脏行作废重取、不合格不落库、空结果不写缓存 | 修复 | §4.4；P0，数据正确性 |
| 熔断器兼容 WAF 412：HTTP 412 且 body 非 JSON 时按 -412 计入滑动窗口（此前仅认 body code，WAF 412 永不计数导致全局暂停失效，实测 16:12 连发 6 次未触发） | 修复 | §7 响应层 b |
| 搜索重试耗尽单歌降级：BiliError 由 matcher 捕获置 MANUAL（fail_reason=SEARCH_FAILED），任务不中断；重试必须重新生成 wts/w_rid；BiliError 消息携带根因 | 修复 | §9.1/§5.2；实测 16:12 单点失败崩整个任务 |
| MatchGate 增补第三闸门 NO_TITLE_MATCH（标题须含歌名 token）与短歌名联合闸（歌名 ≤2 字时标题还须含艺人 token），阈值默认 min_score=16 / min_margin=2 | 功能 | §4.2；实测三批次（62/25/130 首）驱动 |

### v0.4.1 → v0.4.2 变更（起草）

| 变更 | 类型 | 说明 |
| :--- | :--- | :--- |
| 移除废弃的音乐百科 wiki 方法（ncm.py）及测试中 WIKI_URL mock 残留 | 清理 | §5.1/§3.1；降级链早已不再调用，残留曾导致测试维护事故 |

---

## 1. 项目概述

输入一个网易云音乐歌单（≤3000 首），为每首歌在 B 站匹配最合适的视频，并批量添加到 B 站收藏夹。

### 1.1 设计目标

- **正确性优先**：通过多层筛选机制（白名单 / 黑名单 / 两阶段评分 / 降级 / 人工回灌）保证匹配质量。
- **速度可接受**：目标 3000 首全流程 ≤ 20 分钟。注意：该项以搜索为主视角估算（见 §8），收藏阶段按新账号保守默认（并发 1 / 1000ms）约需 60 分钟，老账号调回宽松参数后整体可回落至 20~30 分钟；瓶颈在 B 站接口响应速度与风控节奏，而非本机算力。
- **任务制：每次运行独立任务，支持手动恢复/清理历史任务**：每次运行自动生成唯一 task_id，任务间数据隔离；支持手动 resume 恢复中断任务、delete 清理历史任务。
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
| 收藏接口 `/x/v3/fav/resource/deal` 需要登录态（`SESSDATA` + `bili_jct`），且 `rid` 须为 av 号 | 用户必须授权；程序通过 `auth` 命令自动捕获并安全存储凭证 |
| B 站有风控（HTTP 412 / body code `-412` / `-702`），高频、规律化请求会触发 | 间隔 jitter + 并发上限 + 分层退避，不可绕过只能尊重 |
| 网易云 `playlist/detail` 返回的 `tracks` 不完整，完整曲目在 `trackIds` | 需二次调用 `song/detail` 批量取详情 |

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
│ 阶段二：逐首匹配（核心，asyncio 并发 4）【task 作用域】           │
│   每首歌的状态机闯关（所有状态/结果绑定 task_id）：              │
│   WHITELIST_BV ─► SEARCH ─► BLACKLIST ─► UPLOADER_WL        │
│   ─► SCORING(两阶段) ─► RETRY(降级关键词) ─► MANUAL          │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段三：批量收藏（B站，并发 1 保守默认，可调）【task 作用域】         │
│   自动建收藏夹（每 900 首一个，见§5.3留余量策略）──► 逐条 deal ──► 报告（绑定 task_id） │
│   （--dry-run 时跳过本阶段，改为 preview_report.html）         │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 阶段四：人工回灌                                               │
│   review.html ──► 页面内填 BV 保存（本地服务）──► manual.json  │
│   ──► 重跑（manual 优先级最高）──► DONE                        │
└─────────────────────────────────────────────────────────────┘

横切：SQLite（缓存+状态机）│ 任务管理（§4.4）│ 分层风控（§7）│ 结构化日志（§12）│ 测试（§13）
```

### 3.1 目录结构

```
ncm2bili/
├── main.py                  # CLI 入口（auth / run / task / report），编排各阶段
├── config.py                # 限速/并发/权重等可调参数（可由 config.yaml 覆盖）
├── ncm.py                   # 网易云模块（歌单/详情）
├── bili_search.py           # B站搜索 + WBI 签名 + buvid3 预取
├── scorer.py                # 两阶段评分
├── matcher.py               # 状态机闯关逻辑（阶段二），所有查询/落盘带 task_id 作用域
├── fav.py                   # 收藏夹创建与批量收藏（阶段三）
├── circuit_breaker.py       # 全局熔断器（滑动窗口 -412/-702 计数 → 暂停+降并发，§7）
├── backoff.py               # 单请求指数退避公共实现（§7 响应层 a，搜索/收藏共用）
├── logging_setup.py         # 日志初始化与 cookie 脱敏 Filter（§12）
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

注：`credentials.db` 与 `cache.db` 复用同一 Schema 建全部表，各库仅使用与自身职责相关的表；冗余表不影响功能。

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
   ②B站搜索 轮 1 关键词（§4.3："歌名 歌手"）
               │
               ▼
   ③黑名单过滤（标题归一化后含黑名单词 → 淘汰并记因）
               │
               ▼
   ④uploaders.json 命中 mid 且标题含歌名?   是 ──► DONE (method=UPLOADER_WL)
               │ 否
               ▼
    ⑤两阶段评分 ──► 有合格候选（通过 §4.2 置信度准入）?   是 ──► DONE (method=SCORED)
               │ 否
               ▼
   ⑥降级关键词重试：按 §4.3 降级链逐轮重试，成功回 ③
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

**置信度准入（MatchGate）**：

打分排序后、写库前，对 top1 候选做双闸门校验，任一不满足即不采纳：

- `top1.score < match.min_score`（低置信）→ MANUAL
- `top1.score - top2.score < match.min_margin`（头部不分伯仲）→ MANUAL

不采纳时：bvid 置空，`fail_reason` 记 `LOW_CONFIDENCE: top1={s1} top2={s2}`；`score_detail`（含 top3 候选）完整落库，供 review.html 展示与人工裁决。

参数 config 驱动（`config.yaml` 的 `match` 段，默认值以 config.yaml 为准，代码与 docstring 禁止硬编码）。WHITELIST_BV / UPLOADER_WL 路径不经打分，不适用本门槛。MANUAL 语义不变，review/resume 现有路径直接复用。

设计动机（实测一，2026-09-22）：62 首真实歌单错配率约 37%，错配全部源于低分或近分候选被直接采纳——top1 仅 9.9 分照收（in heat.）；正确候选在 #2 且分差 <2 分（Graveyard Phonk，17.87 vs 15.87）。
第三闸门（相关性前置）：top1 候选标题归一化（去 HTML 标签/实体、小写、去标点）后须包含歌名主体 token，否则 MANUAL（fail_reason=NO_TITLE_MATCH），先于分数/margin 判定。歌名 ≤2 个 CJK 字符时追加联合条件：标题还须包含艺人 token 之一，否则同判 MANUAL——防"我们/海胆/黑洞/四季"类泛词短歌名命中同词非歌内容。token 匹配规则（全包含/逐 token/边界）以代码实现为准并有单测覆盖。

### 4.3 关键词降级链

降级链由 `config.yaml` 的 `degrade.keywords` 驱动（支持 `{name}`/`{artist}` 占位符），默认 9 轮，按"精准 → 泛化 → 特化"递进：

| 类别 | 代表关键词 | 目的 |
| :--- | :--- | :--- |
| 精准 | `歌名 歌手`、`歌手 歌名` | 默认与兼容两种语序 |
| 泛化 | `歌名` | 歌手名生僻或有误时兜底 |
| 特化 | `歌名 MV`、`歌名 纯享`、`歌名 4K`、`歌名 修复`、`歌名 完整版`、`歌名 歌词` | 定向命中官方 MV / 综艺纯净版 / 高清修复 / 完整版 / 字幕版 |

完整轮次、顺序与关键词以 `config.yaml` 为准，修改后无需改代码；按 §4.1 ⑥ 逐轮重试，任一轮产出合格候选即回到主流程，不再继续后续轮次。

**关键词卫生（sanitize）**：

构造/降级任何搜索关键词前，统一经过 sanitize 函数处理（全管线无旁路）：

1. **括号注释剥离**：全/半角括号内为 CJK 描述性文字（用户自定义标注，如 `（压迫感）`、`"炙热"`）整段删除；已知版本 token 保留（Radio Edit/Mix、Remix、Slowed、Speed Up、slowed+reverb、feat./ft.、Official 等），清单入 `config.yaml` 的 `degrade.keep_tokens`；
2. **追加词仅出自降级链**：除 `degrade.keywords` 模板外，代码内禁止任何硬编码追加修饰词；
3. **remix 信息优先保留**：歌名中的 remix/bootleg/版本信息在降级时优先于歌名本体保留，防止匹配回原曲或同名他曲（实测一病例：WINTER FEELS LIKE FUNK 降级后匹配回 JVKE 原曲；MiyaGi-I Got Love remix 匹配回原曲）。

动机：全部来自实测一（2026-09-22）错配病例——`Constriction2.0（压迫感）` 命中"巨物的压迫感"；"…纯享"组合命中 LE SSERAFIM《CELEBRATION》。

### 4.4 任务与缓存

- **断点数据按任务隔离**：每首歌的状态跃迁落盘至 `songs` 表，新增 `task_id` 列（§6），每个运行周期生成唯一 `task_id`，所有状态、评分、缓存结果均绑定当前任务，互不干扰。
- **resume 语义**：`python main.py task resume <task_id>` 加载指定任务的断点状态：已 DONE 的歌直接跳过（不重复发请求）；`FAV_FAILED` 的歌只重跑阶段三——直接取已匹配的 bvid 重新 deal，不重进阶段二、不重复搜索请求。
- **delete 语义**：`python main.py task delete <task_id>` 物理删除指定任务的所有状态与中间结果（`songs` 中该 `task_id` 的行及 `tasks` 表对应行）。`search_cache` 不受 delete 影响（跨任务共享，见同节），如需清理须手动。
- **search_cache 跨任务共享**：搜索缓存以**关键词（keyword）**为 key（`search_cache` 表，TTL 7 天），缓存 key 与 `task_id` 无关——不同任务对同一关键词的搜索结果可复用，调整评分权重后重跑只重打分、不重搜索。
- **--refresh 行为**：仅清除当前任务的匹配结果（`songs` 中 `status` 回退为 PENDING），保留 `search_cache` 和歌单抓取数据，resume 时可直接利用缓存重新匹配。
- **增量语义**：歌单新增歌曲时，旧歌曲因 `songs.status='DONE'` 被断点续跑直接跳过（不发任何请求），只有新歌走完整状态机。`search_cache` 的作用是跨任务复用搜索结果、避免重复搜索请求，**不直接决定歌曲状态**（按 `task_id` 隔离判定状态）。
-**缓存正确性约束**：cache key 为完整 sanitize 后关键词（禁止截断/过度归一化导致碰撞）；命中缓存的结果对象按 key 隔离，禁止跨 key 复用同一可变对象；搜索返回空结果不得回退复用其他关键词的结果。
**实测记录（2026-09-22）：B 站边缘缓存可能对不同相似查询返回逐字节相同的陈旧内容，归属校验是客户端唯一防线，不得移除。**
---

## 5. 外部接口清单

### 5.1 网易云（无需登录）

| 用途 | 接口 | 备注 |
| :--- | :--- | :--- |
| 歌单曲目 | `GET music.163.com/api/v6/playlist/detail?id=&n=1000&offset=` | `trackIds` 完整；分页至 3000 |
| 歌曲详情 | `POST music.163.com/api/v3/song/detail`，body `c=[{id:..},...]` | 每批 ≤1000 |

从 `song/detail` 免费获得：`name`、`ar`（歌手）、`al`（专辑）、**`alia`（别名）**、**`originSongSimpleData`（翻唱的原曲信息）**。

### 5.2 B 站（需 cookie）

| 用途 | 接口 | 认证 | 备注 |
| :--- | :--- | :--- | :--- |
| WBI key | `GET api.bilibili.com/x/web-interface/nav` | 可选 | 取 img_key/sub_key |
| 搜索视频 | `GET api.bilibili.com/x/web-interface/wbi/search/type?search_type=video&keyword=&page=` | buvid3 | **必须 WBI 签名** |
| 视频详情 | `GET api.bilibili.com/x/web-interface/view?bvid=` | 可选 | 三连细分（阶段二） |
| UP主信息 | `GET api.bilibili.com/x/relation/stat?vmid=` | 可选 | 粉丝数，按 mid 缓存 |
| 创建收藏夹 | `POST api.bilibili.com/x/v3/fav/folder/add` | SESSDATA+bili_jct | body 含 csrf |
| 收藏 | `POST api.bilibili.com/x/v3/fav/resource/deal` | SESSDATA+bili_jct | body: `rid&type=2&add_media_ids&del_media_ids&csrf`；`rid` 为 av 号；实测 2026-09-22 `/add` 已废弃（404） |

**错误码约定**：B 站错误同时体现在 HTTP 状态码与 body 的 `code` 字段。本文统一以 **API code**（body 中的值）为准；412 指 HTTP 状态码，`-412` 指 API code，两者同义均按风控处理。

**WBI 签名算法**：

1. `nav` 取 `img_key + sub_key`；
2. 拼接后按固定表 `MIXIN_KEY_ENC_TAB` 重排，截取 32 位得 `mixin_key`；
3. 参数加 `wts`（当前时间戳）后按字典序拼接 + `mixin_key` 做 MD5 得 `w_rid`；
4. 参数值中 `!'()*` 需转义。

`img_key/sub_key` 缓存于内存 + 落盘 `cache.db`，TTL 约 1 天；遇到 `-403`（签名/时间戳错误）时强制刷新重取一次。

**buvid3 获取**：

1. 主路径：GET https://api.bilibili.com/x/frontend/finger/spi，取 data.b_3 写入 cookie buvid3，同时把 data.b_4 存为 buvid4 备用；
2. 降级路径：spi 请求失败（网络错误/非 0 code）时，回退主页 Set-Cookie 抓取，且抓取前断言 session UA 已配置、不含 python/curl/httpx 子串，不满足则先修正 UA 再请求；
3. 两条路径都失败 → 抛出带明确指引的异常（提示检查网络/UA 配置），禁止静默继续；
4. buvid3 成功后持久化到 credentials.db，重跑时直接复用，不必每次重新获取。

### 5.3 收藏夹拆分策略

- 目标收藏夹上限按 **900/个** 留余量（防并发写入时超限），3000 首拆为 ⌈N/900⌉ 个。
- 命名：`<歌单名> (1)`、`(2)`……，重跑时优先复用已存在的同名夹（按名称查询，取其 `media_id`），不为同一任务重复建夹。
- 建夹失败（如达 99 上限，API code `-400`）→ 停止收藏阶段，输出明确错误与当前进度，支持修复后断点续跑。

---

## 6. 数据模型（SQLite）

```sql
CREATE TABLE songs (
  song_key    TEXT,               -- "歌名|歌手"
  task_id     TEXT,               -- 任务标识，与 song_key 联合主键
  ncm_id      INTEGER,
  name        TEXT,
  artist      TEXT,
  album       TEXT,
  alia        TEXT,               -- JSON 数组
  origin      TEXT,               -- 翻唱原曲信息 JSON
  status      TEXT,               -- PENDING/DONE/MANUAL/FAV_FAILED
  method      TEXT,               -- MANUAL/WHITELIST_BV/UPLOADER_WL/SCORED
  bvid        TEXT,
  score_detail TEXT,              -- 评分明细 JSON（抽查调权重用）
  fail_reason TEXT,
  updated_at  INTEGER,
  PRIMARY KEY (song_key, task_id)
);

CREATE TABLE search_cache (
  keyword    TEXT PRIMARY KEY,
  results    TEXT,                -- 候选 JSON
  fetched_at INTEGER
);

CREATE TABLE tasks (
  task_id     TEXT PRIMARY KEY,
  created_at  INTEGER,
  playlist_id TEXT,
  status      TEXT,               -- RUNNING/DONE/FAILED
  finished_at INTEGER,
  stats       TEXT                -- 统计信息 JSON（total/done/manual 等）
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
| **预防层** | 间隔抖动：每次请求 sleep = 基础间隔 + uniform(jitter) | 搜索：400ms + 100~300ms；收藏：1000ms + 200~400ms（新账号保守默认，老账号可在 `config.yaml` 调回 500ms + 100~200ms） | 避免请求时间间隔规律化 |
| **身份层** | 会话级固定 UA + 完整 header 集合 | UA 从 3 个真实浏览器 UA 中**启动时随机选一个并全程固定**；补齐 sec-ch-ua / Accept-Language / Referer | 模拟真实浏览器；**严禁**在同一 session 内切换 UA（与 SESSDATA 混用是风控特征） |
| **响应层 a** | 单请求指数退避：412/网络错误时该请求重试，间隔 2s→4s→8s，最多 3 次 | 初始 2s，倍数 2，上限 3 次 | 处理瞬时抖动 |
| **响应层 b** | 全局熔断：滑动窗口 60s 内 API code `-412` 达 3 次 → 全局暂停 60s；达 5 次 → 暂停 5min 并降并发 50%、输出 WARNING；`-702`（"请求频率过高"）与 `-412` 同级入熔断，但退避基数 2 倍（4s→8s→16s）；阶段三内连续 2 次 `-702` → 剩余请求 interval 翻倍（上限 4 倍）并 WARNING | 窗口 60s；阈值 3/5；降速阈值 2 次、上限 4 倍 | 持续风控时主动冷却 |；HTTP 412 且响应非 JSON（WAF 层拦截）时按 -412 计入同一窗口
| **响应层 c** | 认证失效：API code `-101` / `-111` → 立即中止，提示 `python main.py auth` 重新授权 | — | 凭证类错误不重试 |

收藏写操作沿用更保守参数；所有限速参数集中在 `config.yaml`，可随时调。

---

## 8. 性能估算（3000 首）

| 阶段 | 请求量 | 计算 | 预估耗时 |
| :--- | :--- | :--- | :--- |
| 网易云抓取 | ~10 | 分页 3 次 + song/detail 3 批 | <10s |
| 搜索匹配 | ~3000 × 1.3（降级重试约 30%，随降级链轮次增多而上升，实测后可校准） | 3900 × (0.4s + 0.2s均值抖动) ÷ 并发4 ≈ 585s | 8~13 min |
| 阶段二补查 | ≤3000（差值明显时跳过，预估实际命中 30%） | 900 × 0.5s ÷ 4 ≈ 113s | 含在上项 |
| 收藏 | 3000 | 3000 × (1.0s + 0.3s均值抖动) ÷ 并发1 ≈ 3900s | 默认保守档；老账号调回并发2/500ms ≈ 10 min |
| 风控退避开销 | — | 按 5% 请求触发一次 60s 暂停估算 | +1~2 min |
| **合计（默认保守档）** | | | **~70 min**（收藏为主；老账号调回并发2/500ms 后 ≈ 20~30 min） |

---

## 9. 错误处理

### 9.1 风控（412 / -412 / -702）

见 §7 响应层：单请求指数退避（a）处理瞬时抖动，全局熔断（b）处理持续风控。412 / `-702` **不视为单首歌失败**，重试耗尽才降级记录。搜索请求重试耗尽（BiliError）按单首歌降级处理：status 置 MANUAL，fail_reason 记 SEARCH_FAILED，任务继续执行不中断；重试每次重新生成 wts/w_rid。

### 9.2 认证失效（-101 / -111）

立即中止当前阶段，提示重新执行 `python main.py auth`。此类错误不做退避重试。

### 9.3 收藏幂等

`/x/v3/fav/resource/deal` 对"已在夹中"返回 code `0`（幂等提示，2026-09-21 实现起按 code 0 判定）。程序必须区分三分支：

- **已收藏（code `0`）**：视为成功，状态置 DONE，不记 fail_reason；
- **视频不存在/被删（`-404` / `62002`）**：状态回 MANUAL 并注明，进入人工队列；
- **其他失败**：状态回 **`FAV_FAILED`** 并记 `fail_reason`（单首歌永不使整体任务失败）；`task resume` 时只对 FAV_FAILED 重跑阶段三（§4.4），成功即回 DONE。

### 9.4 凭证安全

- cookie 经 Fernet 对称加密后存入 `credentials.db`（文件权限 600），密钥派生自机器特征（如 `/etc/machine-id`）或首次运行时随机生成并存放于用户目录 600 权限文件；
- **任何日志、报告、异常堆栈不得输出完整 cookie**：`SESSDATA`、`bili_jct`、`buvid3` 的 cookie 值一律替换为 `<redacted>`（见 §12），DEBUG 级也不例外；
- 文档建议用户用小号测试。

---

## 10. 人工回灌与 Dry-Run

### 10.1 Dry-Run 模式

- `python main.py run <歌单ID> --dry-run`：执行阶段一、二、四报告，**跳过阶段三**。
- 生成 `output/preview_report.html`，逐首展示：最终候选 BV、命中方式（method）、评分明细、各候选对比。用户确认后再正式运行。
- dry-run 不写 `manual.json`、不建收藏夹，可安全重复执行；亦不重查已 DONE 与 FAV_FAILED 的歌曲（前者已完成匹配，后者问题在收藏侧，重查无意义）。

### 10.2 人工回灌流程

1. 正式跑完后 `output/review.html` 列出所有 **MANUAL 和 FAV_FAILED** 歌曲（FAV_FAILED 附 fail_reason），每首附 B 站搜索跳转链接。
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
# 1. 模拟运行（推荐首次使用）—— 默认新建任务
python main.py run <网易云歌单ID> --dry-run
# 检查 output/preview_report.html，确认匹配质量

# 2. 正式运行 —— 默认新建任务
python main.py run <网易云歌单ID>

# 3. 丢弃匹配结果重跑（保留歌单数据与缓存）
python main.py run <网易云歌单ID> --refresh

# 4. 任务管理子命令
python main.py task list                          # 列出所有任务及状态
python main.py task resume <task_id>              # 恢复指定任务的断点，从失败处继续
python main.py task delete <task_id>              # 删除指定任务的所有状态与中间结果
python main.py task delete --all-finished         # 批量清理所有终态任务
# list / resume / delete 均支持 --yes 跳过 y/N 确认

# 5. 报告子命令（review.html / report.csv）
python main.py report --task-id <task_id>   # 仅报告指定任务的歌曲（默认全部任务）
python main.py report --serve               # 生成 review.html 并启动本地保存服务（§10.3）
```

**任务生命周期**：每次 `run`（无论 `--dry-run` 还是正式运行）自动生成唯一 `task_id` 并写入 `tasks` 表；`resume` 加载指定任务的 `songs` 状态，已 DONE 的歌跳过；`delete` 物理清除该任务的所有数据。`search_cache` 不受 `delete` 影响（跨任务共享，见 §4.4）。

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
- matcher.py：MatchGate 双闸门边界 + 8 个实测一真实错配病例回归（期望判定与病例一致）+ 正向病例不误伤；

### 13.2 集成测试

- 使用 `pytest + respx`（与 httpx 配套）模拟全部外部 API；
- 覆盖：歌单抓取 → 状态机闯关 → dry-run 报告 的完整链路；
- 异常注入：`-412` / `-702` 连续触发（验证熔断与降速）、`-101`/`-111`（验证立即中止）、超时、收藏"已存在"（code 0）与"视频不存在"响应；
- 任务 resume：中途杀掉进程，`task resume <task_id>` 重启验证无重复请求（替代原断点续跑用例）；
- 任务 delete：`task delete <task_id>` 验证该任务的 songs/tasks 行被清除，search_cache 行保留；
- 任务 list：`task list` 验证任务列表及状态展示正确；
- 收藏失败 → FAV_FAILED → `task resume` 验证只重发 deal 请求、无搜索请求，成功后回 DONE。

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
