# ncm2bili · Trae 里程碑提示词手册

> 用法：每完成一个里程碑并验收通过后，再粘贴下一个。提示词中的 `{文档路径}` 替换为技术文档在仓库中的实际路径（建议放 `/docs/技术文档_v0.3.md`）。
> 铁律：一次只给一个里程碑；每步结束要求 Trae 输出"变更文件列表 + 测试结果"。

---

## 开工前准备（自己做，不给 AI）

1. 按文档 §3.1 建好空目录结构和 `requirements.txt`：

```text
httpx
pydantic
cryptography
pyyaml
playwright
pytest
pytest-asyncio
respx
```

2. 执行 `playwright install chromium`。
3. 把技术文档 v0.3 放入 `/docs/` 目录。
4. 建 `config.yaml` 占位文件。

---

## M0：配置 + 日志 + DB 基建

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M0（基建）。
本步只做：config.py、db.py、日志初始化模块（放在 main.py 中或单独 logging_setup.py）。
不要写任何业务逻辑（不要实现 ncm.py / bili_search.py / scorer.py 等）。

要求：
1. config.py 读取 config.yaml，所有可调参数（限速、并发、权重、抖动范围）有文档 §7/§8 中的默认值，支持环境变量覆盖；
2. db.py 严格按文档 §6 的 DDL 建 6 张表（songs / search_cache / uploader_cache / whitelist_bv / credentials / kv_meta），
   提供 execute / query 封装和"每次状态跃迁立即落盘"的 upsert_song 方法；
3. 日志按文档 §12：控制台 INFO+ 简洁格式；文件 logs/app_YYYY-MM-DD.log 全级别；
   必须实现脱敏 Filter：SESSDATA、bili_jct、buvid3 的 cookie 值一律替换为 <redacted>，DEBUG 级也不例外。

验收（全部通过才算完成）：
- [ ] pytest 跑通建表，6 张表结构可查询验证
- [ ] 脱敏 Filter 有单测：日志内容含 "SESSDATA=abcdef123456;" 时输出中只能是 "SESSDATA=<redacted>;"
- [ ] config.yaml 缺省时所有参数等于文档默认值（一条断言）

完成后输出：变更文件列表 + 测试运行结果。
```

---

## M1：网易云抓取（阶段一）

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M1。M0 已完成（config/db/日志可用）。
本步只做：ncm.py。定义 NcmClient 类：fetch_playlist_track_ids、fetch_song_details、fetch_song_wiki 三个方法。

要求：
1. 接口与字段解析严格按文档 §5.1：playlist/detail 用 n=1000&offset= 分页直至取完 trackIds；
2. song/detail 用 POST body c=[{id:...},...]，每批 ≤1000，解析 name/ar/al/alia/originSongSimpleData；
3. fetch_song_wiki 仅提供方法，本步不做调用方；
4. httpx AsyncClient 由外部注入（构造参数传入），方便测试 mock；
5. 先写测试再写实现；所有测试用 respx mock，禁止真实网络请求。

验收（全部通过才算完成）：
- [ ] 2500 首歌单的分页用例：断言 playlist/detail 被调用 3 次且 trackIds 完整拼接
- [ ] tracks 字段不完整但 trackIds 完整时用例：程序只依赖 trackIds
- [ ] song/detail 解析用例：alia 为 JSON 数组、originSongSimpleData 正确入 origin 字段
- [ ] 请求失败重试 2 次后抛出的用例

完成后输出：变更文件列表 + 测试运行结果。
```

---

## M2：WBI 签名 + B 站搜索

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M2。M0/M1 已完成。
本步只做：bili_search.py。BiliSearchClient：wbi 签名、search_videos(keyword)、buvid3 预取。

要求（严格按文档 §5.2）：
1. WBI 签名四步：nav 取 img_key+sub_key → 按 MIXIN_KEY_ENC_TAB 重排截取 32 位 → 参数加 wts 字典序拼接 + mixin_key 做 MD5 得 w_rid → 参数值中 !'()* 转义；
2. img_key/sub_key 缓存在内存 + 落盘 kv_meta 表，TTL 1 天；API code 为 -403 时强制刷新重取一次后重试；
3. 启动时访问 bilibili.com 主页从 Set-Cookie 抓 buvid3，随搜索请求携带；
4. httpx AsyncClient 外部注入；先写测试再写实现；全部用 respx mock。

⚠️ 特别注意：
- WBI 签名的测试向量使用下面这组公开向量，不要自己编造：
  （在此粘贴你从 bilibili-API-collect 等公开资料找到的 img_key/sub_key/参数/期望 w_rid 向量，
   若找不到完整向量，至少验证 mixin_key 重排截取 32 位这一步的已知向量）
- 签名算法必须与公开参考实现逐行对应，实现后逐行注释说明对应文档哪一步。

验收（全部通过才算完成）：
- [ ] mixin_key 生成向量对得上
- [ ] 完整签名 w_rid 向量对得上（如有）
- [ ] -403 时刷新 key 并重试一次（respx 断言 nav 被调 2 次）
- [ ] buvid3 从 Set-Cookie 正确提取
- [ ] 参数含 !'()* 时转义正确

完成后输出：变更文件列表 + 测试运行结果 + 签名实现与文档步骤的对照说明。
```

---

## M3：评分器 + 状态机（阶段二核心）

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M3。M0–M2 已完成。
本步只做：scorer.py、matcher.py。

scorer.py 要求（文档 §4.2）：
1. 阶段一公式完整实现：w1/w2/w3·log10(x+1) + title_bonus + duration_bonus − author_penalty；
   所有阈值（30s/8min/10min、+10/+3/+5/−10/−15）来自 config.py；
2. Top 3 进阶段二；阶段二仅在候选差值小于阈值（config 可调，默认文档未定时取 10%）时调用 view/relation 接口，
   差值明显直接用阶段一结果，不发起额外请求；
3. uploader 粉丝数按 mid 查 uploader_cache 表缓存，全程序同一 mid 只查一次。

matcher.py 要求（文档 §4.1 状态机 + §4.3 降级链 + §4.4 断点续跑）：
4. 七步闯关完整实现：WHITELIST_BV → SEARCH → BLACKLIST → UPLOADER_WL → SCORING → RETRY(≤2轮) → WIKI救场 → MANUAL；
   黑名单在 UPLOADER_WL 之前过滤（白名单免打分不免内容审查）；
5. 优先级铁律：manual.json > whitelist.json > uploaders.json > 评分（文档唯一出处 §4.1，不得另行解释）；
6. 每次状态跃迁立即调用 db.upsert_song 落盘；
7. 降级链 4 轮关键词按文档 §4.3；第 4 轮 wiki 救场仅对走到该轮的歌调用（无批量接口，每首一次，须控制总量）；
8. B站/网易云客户端通过构造参数注入，测试全部 respx mock；先写测试再写实现。

验收（全部通过才算完成）：
- [ ] 四种 method 分支（WHITELIST_BV/UPLOADER_WL/SCORED/MANUAL）各至少一条用例
- [ ] 优先级铁律四条用例：manual 覆盖一切；whitelist 跳过打分；uploaders 命中跳打分；无白名单才走评分
- [ ] 黑名单词在标题中 → 淘汰并记 fail_reason，且黑名单先于 UPLOADER_WL 生效
- [ ] 降级链：轮2/轮3触发条件正确，每轮失败回黑名单过滤
- [ ] 第 4 轮 wiki 仅对失败歌曲调用（respx 断言调用次数）
- [ ] 状态跃迁落盘有断言：闯关中间杀掉（模拟）后从 db 读到的状态正确
- [ ] 阶段二差值明显时不发 view/relation 请求（respx 断言 0 次）

完成后输出：变更文件列表 + 测试运行结果。
```

---

## M4：Dry-Run + 报告（全流程闭环）

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M4。M0–M3 已完成。
本步只做：main.py 的 run 命令骨架（--dry-run 路径）、report.py（preview_report.html + CSV）。

要求：
1. `python main.py run <歌单ID> --dry-run` 执行：阶段一抓取 → 阶段二状态机 → 生成 output/preview_report.html 和 report.csv；
2. --dry-run 严禁调用收藏夹创建/收藏接口；
3. preview_report.html 逐首展示：song_key、最终 BV、method、评分明细（score_detail 展开）、各候选对比；
4. 并发按 config.py：搜索并发 4 + 间隔 jitter（文档 §7）；412 走文档 §7 响应层退避；
5. 集成测试用例：构造 5 首歌的假歌单（respx mock 网易云和 B 站全流程），断言报告文件生成、
   收藏/建夹接口被请求次数为 0。

验收（全部通过才算完成）：
- [ ] 5 首歌集成用例跑通，method 分布正确（构造用例覆盖 4 种 method）
- [ ] respx 断言：dry-run 下创建收藏夹与收藏接口调用次数为 0
- [ ] preview_report.html 含评分明细且可人工阅读（抽查生成内容）
- [ ] 中断重启用例：跑到第 3 首时中断，重启后已 DONE 的歌不再发搜索请求（respx 断言）
- [ ]"dry-run 实现时不得留下硬编码闸门；main.py 的正式路径分支必须存在（可先 raise NotImplementedError 并注明'待 M5 接通'），dry-run 只跳过阶段三调用"；

完成后输出：变更文件列表 + 测试运行结果 + 一份真实 50 首歌单 dry-run 的操作说明。
```

---

## M5："授权 + 收藏 + run 正式路径接线(接通 main.py run 的非 dry-run 分支，调用 fav.fav_songs)

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M5。M0–M4 已完成，dry-run 全流程可跑。
本步只做：auth.py、fav.py，以及 main.py 的 auth 命令。

auth.py 要求（文档 §11.3）：
1. `python main.py auth`：playwright 打开 Chromium 访问 bilibili.com，用户登录后自动检测
   SESSDATA+bili_jct+buvid3 齐备，抓取后 Fernet 加密写入 credentials.db（密钥派生自 /etc/machine-id，
   缺失时随机生成并存于 ~/.ncm2bili/key，权限 600）；
2. `--paste` 回退模式：无桌面环境时提示手动粘贴 cookie 字符串；
3. 重复执行 auth 视为刷新，覆盖旧凭证。

fav.py 要求（文档 §5.3 + §9.3）：
4. 按 900/夹拆分，命名 "<歌单名> (1)/(2)..."，重跑时按名称查询复用已有收藏夹的 media_id；
5. 幂等三分支（先以实测确认"已在夹中"的返回 code，在代码注释中记录确认日期）：
   已收藏 → 视为成功 DONE；视频不存在(-404 等) → 回 MANUAL 并注明；其他失败 → 记 fail_reason 继续；
6. 收藏并发 2~3，间隔 500ms + 100~200ms jitter；412 走文档 §7 响应层；
7. -101/-111 立即中止并提示重新 auth，不做退避重试；
8. 建夹失败（99 上限 -400）停止收藏阶段，保留断点。

验收（全部通过才算完成）：
- [ ] 幂等三分支各一条 respx 用例
- [ ] 3000 首 → 拆 4 夹的用例；重复运行复用已有 media_id 的用例
- [ ] -101 时立即中止且不清断点的用例
- [ ] 凭证加密落盘 + 日志脱敏联合用例：运行后日志与 credentials.db 中无明文 SESSDATA
- [ ] auth 流程人工实测一次（可 --paste 模式）

⚠️ 提醒：真实收藏测试用小号 + 自建测试收藏夹，首次正式跑用 ≤10 首的小歌单。

完成后输出：变更文件列表 + 测试运行结果 + auth 实测记录。
```

---

## M6：review.html 保存服务（阶段四）

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M6。M0–M5 已完成。
本步只做：report.py 的 review.html 生成（MANUAL 歌曲列表 + 搜索跳转链接 + 页内 BV 输入框）、
review_server.py 本地保存服务。

要求（文档 §10.2 + §10.3）：
1. review.html 每首 MANUAL 歌曲含 B 站搜索跳转链接和 BV 号输入框 + 保存按钮；
2. review_server.py：仅监听 127.0.0.1 随机端口，仅接受 POST /save，body {"song_key","bvid"}；
3. BV 号格式校验 ^BV1[a-zA-Z0-9]{9}$，不合法返回 400；
4. 写入 manual.json 采用"读-改-写 + 临时文件 + os.replace"原子替换，防并发写损坏；
5. 服务器不读取/返回任何歌单与 cookie 数据；review.html 不内嵌敏感信息；
6. main.py 增加 `python main.py report` 子命令：生成 review.html 并可选择拉起本地服务。

验收（全部通过才算完成）：
- [ ] 起服务后 POST 合法 BV → manual.json 更新正确；两次连续 POST 文件不损坏
- [ ] 非法 BV 返回 400 且不写文件
- [ ] 服务仅监听 127.0.0.1 的用例（断言绑定地址）
- [ ] 重跑 matcher 后 manual 结果按优先级铁律生效的端到端用例

完成后输出：变更文件列表 + 测试运行结果。
```

---

## M7：收尾 — 断点压测 + 文档对齐 + 发布检查

```text
背景：项目按 /docs/技术文档_v0.3.md 实现，当前做里程碑 M7（收尾）。M0–M6 已完成。
本步任务：

1. 补齐集成测试（文档 §13.2）：
   - 跑到一半模拟中断（抛 KeyboardInterrupt），重启后 respx 断言已 DONE 歌曲零重复请求；
   - 412 连续触发走全局熔断路径：断言暂停与降并发生效；
   - `--refresh` 语义：丢弃匹配结果但保留 search_cache；
2. 代码-文档一致性检查：通读文档 v0.3 全文，逐节核对实现是否有出入，
   有出入时列出差异清单交给我决策（不要自行改文档也不要自行改实现）；
3. 发布检查：
   - README.md（安装/授权/运行/配置，摘自文档 §11）；
   - requirements.txt 与实际 import 一致；
   - .gitignore 排除 credentials.db / cache.db / logs/ / output/ / manual.json；
   - LICENSE（MIT）；
4. 全量测试跑一遍并贴结果，覆盖率报告（pytest-cov）附后。

验收（全部通过才算完成）：
- [ ] 上述 3 条集成用例通过
- [ ] 差异清单输出（允许为空）
- [ ] 发布四项产物齐全
- [ ] 全量测试通过 + 覆盖率 > 80%（scorer/matcher/fav/bili_search 四个核心模块 > 90%）

完成后输出：差异清单 + 全量测试结果 + 覆盖率报告。
```

---

## 使用备忘

| 情况 | 做法 |
| :--- | :--- |
| Trae 实现与文档冲突 | 不要口头改需求；改文档 v0.3 → 追加变更记录 → 让它对齐 |
| Trae 说"已完成" | 要求贴测试输出，并人工抽查 bili_search.py / matcher.py |
| 某步反复失败 | 把该里程碑再切半（如 M3 拆成 scorer / matcher 两步），缩小上下文 |
| 想加文档外功能 | 先写进文档变更记录，再排进新里程碑，拒绝插队 |
| WBI 向量找不到 | 用真实浏览器抓一次 nav 响应 + 搜索请求，手动算出向量给 Trae 验证 |