from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from kalshibot.config import (
    Settings,
    StrategySettings,
    apply_env_overrides,
    ensure_config_file,
    load_settings,
    save_settings,
)
from kalshibot.money import D

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = load_settings(env={})
    assert s.config_path is None
    assert s.kalshi.base_url == "https://api.elections.kalshi.com/trade-api/v2"
    assert s.kalshi.max_rps <= 3
    assert s.account.starting_balance == D(1000)
    assert s.engine.autostart is True and s.engine.tick_s == 30
    assert s.paper.fee_precision == D("0.01")
    assert s.risk.max_spread == D("0.10") and isinstance(s.risk.max_spread, Decimal)
    assert s.strategies == {}
    assert s.server.port == 8765
    assert s.storage.path == "data/kalshibot.sqlite3"


def test_example_file_matches_defaults(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text())
    assert set(raw) == {"kalshi", "account", "engine", "paper", "risk", "strategies", "analytics", "server",
                        "storage", "live"}
    assert raw["live"]["enabled"] is False  # the shipped example never trades real money
    # the shipped strategies, each spelled out as enabled (their built-in default, see
    # tests/test_integration_defaults.py) with every parameter left at the code's default
    names = ["btc15m_favorite", "ladder_favorite", "maker_favorite", "no_basket_arb"]
    assert raw["strategies"] == {n: {"enabled": True, "params": {}} for n in names}
    # never read the developer's real data/trading-mode.json (the dashboard's mode choice)
    s = load_settings(EXAMPLE, env={"KALSHIBOT_LIVE__MODE_PATH": str(tmp_path / "no-such-mode.json")})
    assert s.config_path == EXAMPLE
    defaults = Settings()
    exclude: Any = {"config_path": True, "storage": True, "strategies": True, "live": {"secrets_path", "mode_path"},
                    "paper_storage_path": True, "paper_base_url": True}
    assert s.model_dump(exclude=exclude) == defaults.model_dump(exclude=exclude)
    # the relative default storage path is resolved against the config file's directory
    assert Path(s.storage.path) == (EXAMPLE.parent / defaults.storage.path).resolve()


def test_yaml_file_and_decimal_exactness(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "risk": {"max_spread": 0.07, "daily_loss_limit": 25.5},
        "strategies": {"demo": {"enabled": True, "params": {"edge": 0.02}}},
    }))
    s = load_settings(p, env={})
    assert s.risk.max_spread == Decimal("0.07")
    assert s.risk.daily_loss_limit == Decimal("25.5")
    assert s.risk.max_orders_per_minute == 30  # untouched default
    assert s.strategy("demo").enabled and s.strategy("demo").params == {"edge": 0.02}
    assert s.strategy("missing") == StrategySettings()


def test_config_yaml_in_cwd_is_picked_up(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("account: {starting_balance: 250}\n")
    monkeypatch.chdir(tmp_path)
    s = load_settings(env={})
    assert s.account.starting_balance == D(250)
    assert s.config_path == Path("config.yaml")


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "KALSHIBOT_KALSHI__MAX_RPS": "1.5",
        "KALSHIBOT_ENGINE__AUTOSTART": "false",
        "KALSHIBOT_RISK__MAX_SPREAD": "0.05",
        "KALSHIBOT_SERVER__HOST": "0.0.0.0",
        "KALSHIBOT_STRATEGIES__DEMO__ENABLED": "true",
        "KALSHIBOT_IGNORED": "x",  # not nested -> ignored
        "OTHER": "1",
    }
    s = load_settings(env=env)
    assert s.kalshi.max_rps == 1.5
    assert s.engine.autostart is False
    assert s.risk.max_spread == D("0.05")
    assert s.server.host == "0.0.0.0"
    assert s.strategy("demo").enabled is True


def test_env_beats_file_and_config_env(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("kalshi: {max_rps: 2}\naccount: {starting_balance: 500}\n")
    s = load_settings(env={"KALSHIBOT_CONFIG": str(p), "KALSHIBOT_KALSHI__MAX_RPS": "1"})
    assert s.config_path == p
    assert s.kalshi.max_rps == 1
    assert s.account.starting_balance == D(500)


def test_apply_env_overrides_creates_sections():
    data: dict = {"risk": 5}
    apply_env_overrides(data, {"KALSHIBOT_RISK__MAX_SPREAD": "0.2", "KALSHIBOT_PAPER__FEE_PRECISION": "0.0001"})
    assert data == {"risk": {"max_spread": 0.2}, "paper": {"fee_precision": 0.0001}}


def test_validation_errors(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("kalshi: {max_rps: 0}\n")
    with pytest.raises(ValueError):
        load_settings(p, env={})
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml", env={})
    p.write_text("- a list\n")
    with pytest.raises(ValueError):
        load_settings(p, env={})


def test_unknown_keys_are_kept(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("extras: {spot: {enabled: true}}\nrisk: {new_limit: 3}\n")
    s = load_settings(p, env={})
    assert s.model_extra["extras"] == {"spot": {"enabled": True}}
    assert s.risk.model_extra["new_limit"] == 3


def test_engine_analytics_and_feeds_keys_are_declared(tmp_path, caplog):
    p = tmp_path / "c.yaml"
    p.write_text("engine: {scanner_days_to_close: 1, universe_max_pages: 7, universe_window_rescan_s: 60}\n"
                 "analytics: {min_settled_trades: 50, max_drawdown_pct: 10}\n"
                 "feeds: {crypto: {symbols: [BTC], ttl_s: 3}}\n")
    with caplog.at_level("WARNING"):
        s = load_settings(p, env={})
    assert "unknown key" not in caplog.text
    assert s.engine.scanner_days_to_close == 1 and s.engine.universe_max_pages == 7
    assert s.analytics.min_settled_trades == 50 and s.analytics.max_drawdown_pct == 10
    assert s.feeds == {"crypto": {"symbols": ["BTC"], "ttl_s": 3}}
    d = Settings()
    assert d.engine.scanner_days_to_close == 0.5 and d.analytics.min_settled_trades == 200 and d.feeds == {}


def test_ensure_and_save(tmp_path):
    dst = tmp_path / "config.yaml"
    assert ensure_config_file(dst, EXAMPLE) == dst and dst.exists()
    dst.write_text("account: {starting_balance: 7}\n")
    ensure_config_file(dst, EXAMPLE)  # no overwrite
    s = load_settings(dst, env={})
    assert s.account.starting_balance == D(7)
    s.risk.max_spread = D("0.03")
    save_settings(s, dst)
    again = load_settings(dst, env={})
    assert again.risk.max_spread == D("0.03") and again.account.starting_balance == D(7)


def test_json_dump_uses_floats():
    d = Settings().model_dump(mode="json")
    assert d["risk"]["max_spread"] == 0.1 and isinstance(d["account"]["starting_balance"], float)
    assert "config_path" not in d
