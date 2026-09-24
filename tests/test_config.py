"""config.py 单测：默认值必须严格等于文档 §7/§8（验收 3）。"""
from __future__ import annotations

from config import load_config

# 文档 §7/§8 默认值（与 config.yaml 注释中的文档参数一一对应）
EXPECTED_DEFAULTS = {
    "rate_limit": {
        "search": {"concurrency": 4, "interval_ms": 400, "jitter_ms": [100, 300]},
        # 2026-09-22 补充：收藏默认对新账号保守（-702 限流经验）
        "fav": {"concurrency": 1, "interval_ms": 1000, "jitter_ms": [200, 400]},
        "stage2": {"concurrency": 4, "interval_ms": 400, "jitter_ms": [100, 300]},
    },
    "risk_control": {
        "retry": {"initial_delay_s": 2, "backoff_factor": 2, "max_retries": 3},
        "circuit_breaker": {
            "window_s": 60,
            "threshold_warn": 3,
            "pause_warn_s": 60,
            "threshold_critical": 5,
            "pause_critical_s": 300,
            "concurrency_cut": 0.5,
        },
    },
    "http": {
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ],
        "timeout_s": 15,
        "referer": "https://www.bilibili.com",
    },
    "scoring": {
        "w1_play": 1.0,
        "w2_fav": 1.0,
        "w3_reply": 0.5,
        "title_bonus_name": 10,
        "title_bonus_keyword": 3,
        "duration_bonus": 5,
        "duration_short_penalty": -10,
        "duration_long_penalty": -10,
        "stage2_diff_threshold": 0.10,
        "top_n_stage2": 3,
    },
    "fav": {"per_folder_limit": 900, "name_template": "{playlist_name} ({index})"},
    "cache": {"wbi_keys_ttl_s": 86400, "search_cache_ttl_s": 604800},
    "degrade": {
        "keywords": [
            "{name} {artist}",
            "{artist} {name}",
            "{name}",
            "{name} MV",
            "{name} 纯享",
            "{name} 4K",
            "{name} 修复",
            "{name} 完整版",
            "{name} 歌词",
        ],
        "keep_tokens": [
            "live", "remix", "bootleg", "slowed", "speed up", "sped up",
            "reverb", "feat", "ft", "official", "mv", "radio edit", "mix", "edit",
        ],
    },
    # 文档 §4.2 MatchGate（v0.4.1）：config 驱动，无硬编码
    "match": {"min_score": 16.0, "min_margin": 2.0},
}


def test_config_yaml_missing_defaults_match_docs() -> None:
    """config.yaml 缺省时，所有参数必须等于文档默认值（一条断言）。"""
    cfg = load_config("/nonexistent/config.yaml", env={})
    assert cfg.model_dump() == EXPECTED_DEFAULTS


def test_degrade_keywords_default_is_final_9_rounds() -> None:
    """验收：load_config 默认（无 yaml 覆盖）时 degrade.keywords 等于定稿 9 条，逐字逐序。"""
    cfg = load_config("/nonexistent/config.yaml", env={})
    keywords = cfg.degrade.keywords
    print("degrade.keywords =", keywords)
    assert keywords == [
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


def test_config_yaml_loaded(tmp_path) -> None:
    """存在的 config.yaml 应正确覆盖默认值。"""
    cfg = load_config(tmp_path / "nope.yaml", env={})  # 缺省路径
    assert cfg.rate_limit.fav.concurrency == 1  # 默认（新账号保守）

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "rate_limit:\n"
        "  fav:\n"
        "    concurrency: 3\n"
        "scoring:\n"
        "  w1_play: 2.5\n",
        encoding="utf-8",
    )
    cfg2 = load_config(yaml_path, env={})
    assert cfg2.rate_limit.fav.concurrency == 3
    assert cfg2.scoring.w1_play == 2.5
    # 未覆盖项仍为默认
    assert cfg2.rate_limit.fav.interval_ms == 1000


def test_env_override(tmp_path) -> None:
    """环境变量覆盖（NCM2BILI_ 前缀 + __ 分隔层级）。"""
    env = {
        "NCM2BILI_RATE_LIMIT__SEARCH__CONCURRENCY": "8",
        "NCM2BILI_SCORING__W1_PLAY": "2.0",
        "NCM2BILI_RATE_LIMIT__FAV__JITTER_MS": "50,150",
        "NCM2BILI_HTTP__TIMEOUT_S": "30",
        "NCM2BILI_HTTP__REFERER": "https://www.example.com",
        "UNRELATED_VAR": "ignored",
    }
    cfg = load_config(tmp_path / "nope.yaml", env=env)
    assert cfg.rate_limit.search.concurrency == 8
    assert cfg.scoring.w1_play == 2.0
    assert cfg.rate_limit.fav.jitter_ms == [50, 150]
    assert cfg.http.timeout_s == 30
    assert cfg.http.referer == "https://www.example.com"


# ---- v0.4.1：MatchGate 阈值 config 驱动（无硬编码）----


def test_match_config_defaults_section() -> None:
    """文档 §4.2 MatchGate 默认值：min_score=16 / min_margin=2，config 驱动。"""
    cfg = load_config("/nonexistent/config.yaml", env={})
    assert cfg.match.min_score == 16.0
    assert cfg.match.min_margin == 2.0


def test_match_config_yaml_overrides(tmp_path) -> None:
    """config.yaml 的 match 段覆盖生效。"""
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "match:\n"
        "  min_score: 20\n"
        "  min_margin: 3\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path, env={})
    assert cfg.match.min_score == 20.0
    assert cfg.match.min_margin == 3.0


def test_match_config_env_override(tmp_path) -> None:
    """环境变量 NCM2BILI_MATCH__MIN_SCORE / __MIN_MARGIN 覆盖。"""
    env = {
        "NCM2BILI_MATCH__MIN_SCORE": "18",
        "NCM2BILI_MATCH__MIN_MARGIN": "2.5",
    }
    cfg = load_config(tmp_path / "nope.yaml", env=env)
    assert cfg.match.min_score == 18.0
    assert cfg.match.min_margin == 2.5


def test_degrade_keep_tokens_default_section() -> None:
    """文档 §4.3 sanitize keep_tokens 默认清单（版本信息保留）。"""
    cfg = load_config("/nonexistent/config.yaml", env={})
    assert "live" in cfg.degrade.keep_tokens
    assert "remix" in cfg.degrade.keep_tokens
    assert "纯享" not in cfg.degrade.keep_tokens  # 追加词与保留 token 分离
