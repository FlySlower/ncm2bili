# ncm2bili

网易云音乐歌单 → B 站收藏夹 批量转换工具。

输入一个网易云音乐歌单（≤3000 首），自动为每首歌在 B 站匹配最合适的视频，
并批量添加到 B 站收藏夹。支持断点续跑、人工回灌、dry-run 预览。

> 技术文档：[docs/ncm2bili_技术文档_v0.3.md](docs/ncm2bili_技术文档_v0.3.md)

## 功能特性

- **两阶段评分匹配**：白名单 / 黑名单 / UP 主白名单 / 权重评分 / 关键词降级 / 人工回灌
- **断点续跑**：任意时刻中断，重启后已完成歌曲不重复请求
- **Dry-Run 模式**：先看匹配报告，确认后再正式收藏
- **人工回灌**：review.html 页内填 BV 号保存，重跑后最高优先级生效
- **风控友好**：WBI 签名 + buvid3 预取 + 三层风控（jitter / 指数退避 / 全局熔断）
- **凭证安全**：cookie 经 Fernet 加密存储于 credentials.db

## 安装

要求 Python ≥ 3.11。

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd ncm2bili

# 2. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

# 3. 安装依赖
pip install -r requirements.txt   # 含 httpx, pydantic, playwright, cryptography
playwright install chromium       # auth 功能需要
```

## 授权（首次使用）

B 站搜索与收藏接口需要登录态，先执行授权命令：

```bash
# 自动模式：playwright 打开 Chromium 登录 B 站，自动捕获凭证
python main.py auth

# 无桌面环境（服务器）：手动粘贴 Cookie 字符串
python main.py auth --paste
```

授权成功后 cookie 经 Fernet 加密写入 `credentials.db`。重复执行 auth 视为刷新，
覆盖旧凭证。收到 `-101`（未登录）提示时重新 auth。

## 使用

```bash
# 1. Dry-Run 预览（推荐首次使用）
python main.py run <网易云歌单ID> --dry-run
# 生成 output/preview_report.html 与 report.csv，不调用任何写接口

# 2. 人工回灌（可选）：处理匹配失败的歌曲
python main.py report              # 生成 review.html
python main.py report --serve      # 同时启动本地保存服务（127.0.0.1 随机端口）

# 3. 正式运行：自动建收藏夹并批量收藏
python main.py run <网易云歌单ID>

# 4. 丢弃匹配结果重新匹配（保留搜索缓存）
python main.py run <网易云歌单ID> --refresh
```

### 收藏夹拆分策略

- 目标收藏夹上限 900/个（留余量防并发超限），3000 首拆为 ⌈N/900⌉ 个；
- 命名：`<歌单名> (1)`、`(2)`……，重跑时按名称复用已有收藏夹；
- 建夹失败（达 99 上限，API code -400）→ 停止收藏阶段并保留断点。

## 配置

所有可调参数集中在 [config.yaml](config.yaml)，均有文档默认值（对应技术文档 §7/§8）：

| 类别 | 参数示例 | 默认 |
| :--- | :--- | :--- |
| 限速 | `rate_limit.search.concurrency` / `jitter_ms` | 4 / [100, 300] |
| 收藏 | `rate_limit.fav.concurrency` / `interval_ms` | 2 / 500ms + [100,200] |
| 风控 | `risk_control.retry` / `circuit_breaker` | 2s→4s→8s / 3次暂停60s |
| 评分 | `scoring.w1_play` 等权重 | 1.0 / 1.0 / 0.5 |
| 拆夹 | `fav.per_folder_limit` | 900 |

环境变量覆盖：`NCM2BILI_` 前缀 + `__` 分层，例如 `NCM2BILI_SCORING__W1_PLAY=2.0`。

匹配配置：`whitelist.json` / `uploaders.json` / `blacklist_words.json` / `manual.json`
（均为可选，缺省时使用对应默认值）。

## 日志与安全

- 日志：控制台 INFO+；文件 `logs/app_YYYY-MM-DD.log` 全级别；
- cookie 脱敏铁律：`SESSDATA` / `bili_jct` / `buvid3` 值在日志中一律替换为 `<redacted>`；
- `credentials.db` 为 Fernet 加密存储（密钥派生自机器特征或 `~/.ncm2bili/key`，权限 600）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest                    # 全量测试（全部外部请求由 respx mock）
pytest --cov=ncm2bili --cov-report=term-missing
```

## License

MIT License，见 [LICENSE](LICENSE)。
