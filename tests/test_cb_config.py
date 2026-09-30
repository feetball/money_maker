"""``coinbase:`` settings (kalshibot/coinbase/config.py + the additive field on kalshibot.config.Settings).

Contract §15: defaults when the section is absent (existing config.yaml files keep working),
``KALSHIBOT_COINBASE__...`` env overrides, relative ``storage_path`` resolved like
``storage.path``, and a bad section disabling only Coinbase (never Kalshi).
"""

import logging
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kalshibot.coinbase.config import CoinbaseSettings, CoinbaseStrategySettings
from kalshibot.coinbase.fees import DEFAULT_TIER, FEE_TIERS
from kalshibot.config import Settings, load_settings, save_settings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"
D = Decimal


def test_defaults_match_contract():
    cb = Settings().coinbase
    assert isinstance(cb, CoinbaseSettings)
    assert cb.enabled is True
    assert cb.base_url == "https://api.exchange.coinbase.com"
    assert cb.max_rps == 3 and cb.timeout == 10
    assert cb.starting_balance == D(1000) and isinstance(cb.starting_balance, Decimal)
    assert cb.fee_tier == "intro" and cb.fee_rates is None and cb.tier() is DEFAULT_TIER
    assert cb.storage_path == "data/coinbase.sqlite3"
    assert cb.paper.model_dump() == {"max_slippage_bps": 100, "consumed_liquidity_ttl_s": 300,
                                     "default_gtc_expiry_s": 3600, "fill_on_book_cross": False}
    assert cb.engine.model_dump() == {"autostart": True, "bar_delay_s": 60, "maintenance_s": 15, "snapshot_s": 60,
                                      "products_refresh_s": 3600, "maker_timeout_s": 120}
    assert cb.risk.model_dump() == {
        "max_position_pct_per_product": 50, "max_total_exposure_pct": 90, "max_strategy_allocation_pct": 50,
        "min_cash_reserve": D(20), "max_orders_per_minute": 20, "daily_loss_limit": D(100),
        "max_spread_bps": 50, "min_trade_usd": D(10)}
    assert cb.strategies == {} and cb.load_error is None
    assert cb.strategy("anything") == CoinbaseStrategySettings()


def test_defaults_without_any_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = load_settings(env={})
    assert s.config_path is None
    assert s.coinbase == CoinbaseSettings()
    assert s.coinbase.storage_path == "data/coinbase.sqlite3"  # nothing to resolve against


def test_config_yaml_without_coinbase_section_loads(tmp_path, caplog):
    """An existing (pre-Coinbase) config.yaml keeps working unchanged."""
    cfg = tmp_path / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "kalshi: {max_rps: 2}\naccount: {starting_balance: 500}\nstrategies: {}\n"
        "storage: {path: data/kalshibot.sqlite3}\n"
    )
    with caplog.at_level(logging.WARNING, logger="kalshibot.config"):
        s = load_settings(cfg, env={})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert s.kalshi.max_rps == 2 and s.account.starting_balance == D(500)
    assert s.coinbase.enabled is True and s.coinbase.load_error is None
    # the Coinbase account is separate: its own balance, not Kalshi's
    assert s.coinbase.starting_balance == D(1000)
    # relative storage_path resolves against the config file's directory, like storage.path
    assert Path(s.coinbase.storage_path) == (cfg.parent / "data" / "coinbase.sqlite3").resolve()
    assert Path(s.storage.path).parent == Path(s.coinbase.storage_path).parent
    # ... but a dump keeps the path as configured
    assert s.model_dump()["coinbase"]["storage_path"] == "data/coinbase.sqlite3"


def test_commented_or_empty_section_means_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("coinbase:\n  # enabled: false\n")  # parses as None -> treated as absent
    s = load_settings(p, env={})
    assert s.coinbase.enabled is True and s.coinbase.load_error is None
    p.write_text("coinbase: {}\n")
    assert load_settings(p, env={}).coinbase.fee_tier == "intro"


def test_example_file_loads_coinbase_defaults(caplog):
    with caplog.at_level(logging.WARNING, logger="kalshibot.config"):
        s = load_settings(EXAMPLE, env={})
    assert not [r for r in caplog.records if "coinbase" in r.getMessage()]
    assert s.coinbase.model_dump() == CoinbaseSettings().model_dump()


def test_example_documents_every_default():
    """The commented ``# coinbase:`` block in config.example.yaml, uncommented, equals the defaults
    and has no unknown keys."""
    lines = EXAMPLE.read_text().splitlines()
    start = lines.index("# coinbase:")
    block = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(re.sub(r"^# ?", "", line))
    data = yaml.safe_load("\n".join(block))["coinbase"]
    cb = CoinbaseSettings.model_validate(data)
    assert cb.model_dump() == CoinbaseSettings().model_dump()

    def no_extra(m):
        assert not m.model_extra, m.model_extra
        for v in m.__dict__.values():
            if hasattr(v, "model_extra"):
                no_extra(v)

    no_extra(cb)
    # every field of every section is documented
    assert set(data) >= set(CoinbaseSettings.model_fields) - {"load_error", "fee_rates"}
    for sec in ("paper", "engine", "risk"):
        assert set(data[sec]) == set(type(getattr(cb, sec)).model_fields), sec


def test_yaml_overrides_and_decimal_exactness(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"coinbase": {
        "starting_balance": 2500.5,
        "fee_tier": "Intro EU",
        "risk": {"min_cash_reserve": 20.1, "max_spread_bps": 25},
        "paper": {"max_slippage_bps": 40},
        "engine": {"autostart": False},
        "strategies": {"trend": {"enabled": True, "params": {"lookback": 50}, "max_allocation_pct": 30}},
    }}))
    s = load_settings(p, env={})
    cb = s.coinbase
    assert cb.starting_balance == D("2500.5")
    assert cb.fee_tier == "intro_eu" and cb.tier() is FEE_TIERS["intro_eu"]
    assert cb.risk.min_cash_reserve == D("20.1") and cb.risk.max_spread_bps == 25
    assert cb.risk.max_orders_per_minute == 20  # untouched default
    assert cb.paper.max_slippage_bps == 40 and cb.engine.autostart is False
    st = cb.strategy("trend")
    assert st.enabled is True and st.params == {"lookback": 50} and st.max_allocation_pct == 30


def test_explicit_fee_rates_win(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("coinbase: {fee_tier: intro_eu, fee_rates: {maker: 0.004, taker: 0.008}}\n")
    t = load_settings(p, env={}).coinbase.tier()
    assert t.name == "custom" and t.maker_rate == D("0.004") and t.taker_rate == D("0.008")


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "scratch" / "cb.sqlite3"
    env = {
        "KALSHIBOT_COINBASE__ENABLED": "false",
        "KALSHIBOT_COINBASE__MAX_RPS": "2",
        "KALSHIBOT_COINBASE__STORAGE_PATH": str(db),
        "KALSHIBOT_COINBASE__FEE_TIER": "Intro-Pre-2026-09",
        "KALSHIBOT_COINBASE__FEE_RATES__TAKER": "0.01",
        "KALSHIBOT_COINBASE__RISK__MAX_SPREAD_BPS": "30",
        "KALSHIBOT_COINBASE__ENGINE__AUTOSTART": "false",
        "KALSHIBOT_COINBASE__STRATEGIES__MOMO__ENABLED": "true",
        "KALSHIBOT_KALSHI__MAX_RPS": "1",
    }
    s = load_settings(env=env)
    cb = s.coinbase
    assert cb.enabled is False and cb.load_error is None
    assert cb.max_rps == 2 and s.kalshi.max_rps == 1
    assert cb.storage_path == str(db)
    assert cb.fee_tier == "intro_pre_2026_09"
    assert cb.fee_rates is not None and cb.fee_rates.taker == D("0.01") and cb.fee_rates.maker == D("0.005")
    assert cb.risk.max_spread_bps == 30 and cb.engine.autostart is False
    assert cb.strategy("momo").enabled is True


def test_env_storage_path_with_config_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("server: {port: 8771}\n")
    db = tmp_path / "abs" / "cb.sqlite3"
    s = load_settings(p, env={"KALSHIBOT_COINBASE__STORAGE_PATH": str(db)})
    assert s.coinbase.storage_path == str(db)  # absolute paths are kept
    p.write_text('coinbase: {storage_path: ":memory:"}\n')
    assert load_settings(p, env={}).coinbase.storage_path == ":memory:"


@pytest.mark.parametrize(
    ("section", "needle"),
    [
        ({"fee_tier": "platinum"}, "fee_tier"),
        ({"max_rps": -1}, "max_rps"),
        ({"risk": {"max_total_exposure_pct": 150}}, "risk.max_total_exposure_pct"),
        ({"starting_balance": "lots"}, "starting_balance"),
        ({"fee_rates": {"maker": 2}}, "fee_rates.maker"),
        ("garbage", "coinbase"),
    ],
)
def test_invalid_section_disables_only_coinbase(tmp_path, caplog, section, needle):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"kalshi": {"max_rps": 2}, "coinbase": section}))
    with caplog.at_level(logging.ERROR, logger="kalshibot.config"):
        s = load_settings(p, env={})
    assert s.kalshi.max_rps == 2  # Kalshi loads normally
    assert s.coinbase.enabled is False
    assert s.coinbase.load_error and s.coinbase.load_error.startswith("invalid coinbase config")
    assert needle in s.coinbase.load_error
    assert any("Coinbase venue disabled" in r.getMessage() for r in caplog.records)


def test_invalid_env_override_disables_only_coinbase(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = load_settings(env={"KALSHIBOT_COINBASE__MAX_RPS": "0", "KALSHIBOT_ACCOUNT__STARTING_BALANCE": "700"})
    assert s.account.starting_balance == D(700)
    assert s.coinbase.enabled is False and "max_rps" in (s.coinbase.load_error or "")


def test_kalshi_validation_errors_still_raise(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("kalshi: {max_rps: -1}\ncoinbase: {max_rps: 2}\n")
    with pytest.raises(ValueError):
        load_settings(p, env={})


def test_save_round_trip_keeps_configured_path(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("coinbase: {storage_path: dbs/cb.sqlite3, fee_tier: vip_8, risk: {min_trade_usd: 12.5}}\n")
    s = load_settings(p, env={})
    assert Path(s.coinbase.storage_path) == (tmp_path / "dbs" / "cb.sqlite3").resolve()
    out = save_settings(s, tmp_path / "saved.yaml")
    raw = yaml.safe_load(out.read_text())["coinbase"]
    assert raw["storage_path"] == "dbs/cb.sqlite3"
    assert "load_error" not in raw
    assert raw["risk"]["min_trade_usd"] == 12.5 and raw["fee_rates"] is None
    s2 = load_settings(out, env={})
    assert s2.coinbase.model_dump() == s.coinbase.model_dump()
    assert s2.coinbase.storage_path == s.coinbase.storage_path


def test_reassigned_storage_path_is_dumped_as_is(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("server: {port: 8772}\n")
    s = load_settings(p, env={})
    s.coinbase.storage_path = str(tmp_path / "other.sqlite3")
    assert s.model_dump()["coinbase"]["storage_path"] == str(tmp_path / "other.sqlite3")


def test_json_dump_is_serializable():
    d = Settings().model_dump(mode="json")["coinbase"]
    assert d["starting_balance"] == 1000.0 and d["risk"]["min_cash_reserve"] == 20.0
    assert "load_error" not in d


@pytest.mark.parametrize("first", ["kalshibot.coinbase.config", "kalshibot.config"])
def test_import_order_has_no_cycle(first):
    code = f"import {first}; import kalshibot.config as c; print(c.Settings().coinbase.fee_tier)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "intro"


def test_coinbase_import_failure_never_breaks_kalshi_settings(tmp_path):
    """If kalshibot.coinbase.config cannot be imported, Kalshi settings still load and the
    Coinbase venue reports itself disabled with the reason."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kalshi: {max_rps: 2}\ncoinbase: {max_rps: 1, storage_path: x.sqlite3}\n")
    code = (
        "import sys; sys.modules['kalshibot.coinbase.config'] = None\n"
        "from kalshibot.config import load_settings\n"
        f"s = load_settings({str(cfg)!r}, env={{}})\n"
        "print(s.kalshi.max_rps, s.coinbase.enabled, s.coinbase.load_error.split(':')[0])\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "2.0 False coinbase settings unavailable"
