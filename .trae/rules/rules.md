---
alwaysApply: false
description: 仅在ncm2bili中生效
---
# 项目规则（AI 必须遵守）
1. 一切设计决策以 docs/ncm2bili_技术文档_v0.3.md 为准；实现与文档冲突时，停止并向我报告，不得自行修改文档或设计。
2. 一次只实现当前里程碑指定的模块，不越界写其他文件。
3. 先写测试再写实现；所有外部网络请求必须用 respx mock，测试禁止真实出站。
4. httpx AsyncClient 一律通过构造参数注入，不允许在业务模块内全局创建。
5. 优先级铁律：manual.json > whitelist.json > uploaders.json > 评分（唯一出处：文档 §4.1）。
6. 日志/异常/报告中严禁出现明文 cookie（SESSDATA / bili_jct / buvid3）。
7. 每次状态机跃迁必须立即落盘 SQLite。8. Python ≥ 3.11，代码风格遵循 pyproject.toml 中的 ruff 配置。