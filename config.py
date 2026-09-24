"""配置加载模块（里程碑 M0）。

加载优先级（低 → 高）：
    内置默认值（文档 §7/§8） < config.yaml < 环境变量（NCM2BILI_ 前缀）
环境变量路径用 ``__`` 分隔层级，例如：NCM2BILI_SCORING__W1_PLAY=2.0
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# 默认配置文件路径（项目根目录）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# 环境变量前缀与层级分隔符
ENV_PREFIX = "NCM2BILI_"
ENV_SEP = "__"

# 文档 §7 身份层：3 个真实浏览器 UA（启动时随机选一个并全程固定）
_DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",  # noqa: E501
]


class RateLimitSection(BaseModel):
    """单类请求的并发与间隔（文档 §7 预防层）。"""

    concurrency: int = 4
    interval_ms: int = 400
    jitter_ms: list[int] = Field(default_factory=lambda: [100, 300])


class RateLimitConfig(BaseModel):
    """限速配置：搜索 / 收藏（写操作更保守）/ 阶段二补查。"""

    search: RateLimitSection = Field(default_factory=RateLimitSection)
    # 收藏默认对新账号保守（文档 §7 + 2026-09-22 补充 -702 限流经验）：
    # concurrency=1，interval 1000ms + 200~400ms jitter，老账号可自行调回
    fav: RateLimitSection = Field(
        default_factory=lambda: RateLimitSection(
            concurrency=1, interval_ms=1000, jitter_ms=[200, 400]
        )
    )
    stage2: RateLimitSection = Field(default_factory=RateLimitSection)


class RetryConfig(BaseModel):
    """单请求指数退避（文档 §7 响应层 a）。"""

    initial_delay_s: float = 2
    backoff_factor: float = 2
    max_retries: int = 3


class CircuitBreakerConfig(BaseModel):
    """全局熔断（文档 §7 响应层 b）。"""

    window_s: int = 60
    threshold_warn: int = 3
    pause_warn_s: int = 60
    threshold_critical: int = 5
    pause_critical_s: int = 300
    concurrency_cut: float = 0.5


class RiskControlConfig(BaseModel):
    """风控响应层（文档 §7）。"""

    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


class HttpConfig(BaseModel):
    """身份层：会话级固定 UA + 完整 header 集合（文档 §7）。"""

    user_agents: list[str] = Field(default_factory=lambda: list(_DEFAULT_USER_AGENTS))
    timeout_s: float = 15
    referer: str = "https://www.bilibili.com"


class ScoringConfig(BaseModel):
    """两阶段评分权重（文档 §4.2 / §8）。"""

    w1_play: float = 1.0
    w2_fav: float = 1.0
    w3_reply: float = 0.5
    title_bonus_name: float = 10
    title_bonus_keyword: float = 3  # "官方/原唱/MV/音频/歌词/完整版" 每项
    duration_bonus: float = 5       # 1~8 分钟
    duration_short_penalty: float = -10  # <30s
    duration_long_penalty: float = -10   # >10min
    stage2_diff_threshold: float = 0.10  # 候选差值 <10% 才进阶段二精排
    top_n_stage2: int = 3


class FavConfig(BaseModel):
    """收藏夹拆分策略（文档 §5.3）。"""

    per_folder_limit: int = 900
    name_template: str = "{playlist_name} ({index})"


class CacheConfig(BaseModel):
    """缓存 TTL。"""

    wbi_keys_ttl_s: int = 86400      # img_key/sub_key 缓存 1 天
    search_cache_ttl_s: int = 604800  # 搜索缓存 7 天


class DegradeConfig(BaseModel):
    """降级链关键词与关键词卫生（文档 §4.3，支持 {name}/{artist} 占位符）。

    keep_tokens：括号内版本信息保留清单（文档 §4.3 sanitize 规则 1）——
    括号注释剥离时，括号内容含任一 token（不区分大小写）则整段保留
    （Live / Remix / Slowed / Radio Edit / feat. 等均为版本描述而非用户注释）。
    """

    keywords: list[str] = Field(
        default_factory=lambda: [
            "{name} {artist}",
            "{artist} {name}",
            "{name}",
            "{name} MV",
            "{name} 纯享",
            "{name} 4K",
            "{name} 修复",
            "{name} 完整版",
            "{name} 歌词",
        ]
    )
    keep_tokens: list[str] = Field(
        default_factory=lambda: [
            "live",
            "remix",
            "bootleg",
            "slowed",
            "speed up",
            "sped up",
            "reverb",
            "feat",
            "ft",
            "official",
            "mv",
            "radio edit",
            "mix",
            "edit",
        ]
    )


class MatchConfig(BaseModel):
    """匹配置信度准入（文档 §4.2 MatchGate，实测一错配率驱动）。

    - min_score：top1 低于该分 → 低置信不采纳（置 MANUAL）；
    - min_margin：top1 与 top2 分差小于该值 → 头部不分伯仲不采纳（置 MANUAL）。
    两闸门在 NO_TITLE_MATCH（含短歌名联合闸）之后、打分采纳之前依次判定。
    """

    min_score: float = 16.0
    min_margin: float = 2.0


class Config(BaseModel):
    """全量配置模型，所有字段均有文档默认值。"""

    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    risk_control: RiskControlConfig = Field(default_factory=RiskControlConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    fav: FavConfig = Field(default_factory=FavConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    degrade: DegradeConfig = Field(default_factory=DegradeConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)


def _coerce_env(value: str) -> Any:
    """宽松解析环境变量字符串：布尔 / 逗号分隔列表 / 数值 / 原样字符串。"""
    v = value.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if "," in v:
        return [_coerce_env(part) for part in v.split(",")]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _set_nested(data: dict, keys: list[str], value: Any) -> None:
    """按 keys 路径写入嵌套 dict（环境变量覆盖用）。"""
    cur = data
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
    cur[keys[-1]] = value


def _deep_merge(base: dict, override: dict) -> dict:
    """递归深度合并：子 dict 逐层覆盖，保留 base 中未被覆盖的字段。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    """加载配置：默认值 <- config.yaml <- 环境变量。

    - 默认值先由 Config() 构造（文档 §7/§8），yaml / 环境变量做深度合并，
      部分覆盖不会丢失同节其他默认字段（如 fav 专用间隔 500ms）。
    - Args:
        path: config.yaml 路径；为 None 时用项目根目录的 config.yaml；
              文件不存在时全部使用文档默认值。
        env: 环境变量字典；为 None 时取 os.environ（测试可注入）。
    """
    if env is None:
        env = os.environ

    data: dict[str, Any] = {}
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件格式错误（应为 YAML 映射）：{config_path}")
        data = loaded

    for env_key, env_val in env.items():
        if env_key.startswith(ENV_PREFIX):
            key_path = env_key[len(ENV_PREFIX):].lower().split(ENV_SEP)
            _set_nested(data, key_path, _coerce_env(env_val))

    merged = _deep_merge(Config().model_dump(), data)
    return Config(**merged)
