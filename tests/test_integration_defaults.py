"""Integration defaults: which strategies run without config, and config.example.yaml matching the code.

* ``enabled`` precedence: dashboard toggle (store) > ``strategies.<name>.enabled`` (config/env) >
  the class's ``enabled_by_default``; ``/api/strategies`` reports the source.
* The four research strategies are on by default, so a ``config.yaml`` written before they existed
  (``strategies: {}``) runs them after a restart.
* ``config.example.yaml`` lists all four (enabled), and every parameter / risk value it documents in
  a comment equals the code's default (so the comments cannot drift from what actually runs).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from conftest import DummyStrategy, FakeKalshiClient

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings, StrategySettings, apply_env_overrides, load_settings
from kalshibot.feeds import FeedRegistry
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.base import Strategy

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"
RESEARCH = ("btc15m_favorite", "ladder_favorite", "maker_favorite", "no_basket_arb")


class DefaultOn(DummyStrategy):
    name = "default_on"
    enabled_by_default: ClassVar[bool] = True


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any]) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


def test_strategy_settings_enabled_is_unset_by_default() -> None:
    assert StrategySettings().enabled is None
    assert Settings().strategy("anything").enabled is None
    assert Strategy.enabled_by_default is False


async def test_enabled_precedence(settings: Settings, fake_client: FakeKalshiClient) -> None:
    strategies = {"dummy": DummyStrategy, "default_on": DefaultOn}
    svc = stack(settings, fake_client, strategies)
    eng = svc.engine
    # nothing configured: the class default decides
    assert not eng.runtimes["dummy"].enabled and eng.runtimes["dummy"].enabled_source == "default"
    assert eng.runtimes["default_on"].enabled and eng.runtimes["default_on"].enabled_source == "default"
    assert set(eng.md.specs) == {"default_on"}
    js = eng.strategy_json("default_on")
    assert js["enabled"] is True and js["enabled_source"] == "default"
    # the dashboard switch is saved and wins from then on
    eng.update_strategy("default_on", enabled=False)
    assert not eng.runtimes["default_on"].enabled and eng.runtimes["default_on"].enabled_source == "dashboard"
    await svc.aclose()

    svc2 = stack(settings, FakeKalshiClient(), strategies)
    rt = svc2.engine.runtimes["default_on"]
    assert not rt.enabled and rt.enabled_source == "dashboard"
    await svc2.aclose()


async def test_config_beats_class_default(tmp_path: Path, fake_client: FakeKalshiClient) -> None:
    for i, (cfg, expect) in enumerate([({"enabled": False}, False), ({"enabled": True}, True), ({}, True)]):
        s = Settings.model_validate({"strategies": {"default_on": cfg}})
        s.storage.path = str(tmp_path / f"db{i}.sqlite3")
        svc = stack(s, FakeKalshiClient(), {"default_on": DefaultOn})
        rt = svc.engine.runtimes["default_on"]
        assert rt.enabled is expect
        assert rt.enabled_source == ("config" if "enabled" in cfg else "default")
        await svc.aclose()


def test_env_override_disables_a_default_on_strategy() -> None:
    data = apply_env_overrides({}, {"KALSHIBOT_STRATEGIES__MAKER_FAVORITE__ENABLED": "false"})
    s = Settings.model_validate(data)
    assert s.strategy("maker_favorite").enabled is False
    assert s.strategy("btc15m_favorite").enabled is None


def test_research_strategies_are_on_by_default() -> None:
    for name in RESEARCH:
        assert REGISTRY[name].enabled_by_default is True, name


async def test_old_config_without_strategies_runs_all_four(tmp_path: Path) -> None:
    """A config.yaml written from the old example (``strategies: {}``) enables every research strategy."""
    p = tmp_path / "config.yaml"
    p.write_text("strategies: {}\nstorage: {path: data/test.sqlite3}\n")
    s = load_settings(p, env={})
    svc = build_services(s, client=FakeKalshiClient(), feeds=FeedRegistry())
    try:
        for name in RESEARCH:
            rt = svc.engine.runtimes[name]
            assert rt.enabled and rt.enabled_source == "default", name
        assert svc.md.specs["btc15m_favorite"].series_tickers == ["KXBTC15M"]
        # one close-time window for all of them: none asks for more than 3 days
        assert max(sp.max_days_to_close or 0 for sp in svc.md.specs.values()) == 3
    finally:
        await svc.aclose()


def test_example_config_lists_all_four_enabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="kalshibot.config"):
        s = load_settings(EXAMPLE, env={})
    assert not [r for r in caplog.records if "unknown key" in r.getMessage()]
    for name in RESEARCH:
        st = s.strategy(name)
        assert st.enabled is True and st.params == {}, name
        assert st.max_allocation_pct is None and st.daily_loss_limit is None, name  # class risk_defaults apply
    assert set(s.strategies) == set(RESEARCH)


def _documented(name: str) -> dict[str, Any]:
    """``# key: value`` comment lines inside ``strategies.<name>`` of the example file."""
    lines = EXAMPLE.read_text().splitlines()
    start = lines.index(f"  {name}:")
    out: dict[str, Any] = {}
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line) or re.match(r"^\S", line):
            break
        m = re.match(r"^\s*#\s+([a-z_0-9]+):\s*(.+?)\s*(#.*)?$", line)
        if m:
            out[m.group(1)] = yaml.safe_load(m.group(2))
    return out


@pytest.mark.parametrize("name", RESEARCH)
def test_example_comments_match_code_defaults(name: str) -> None:
    cls = REGISTRY[name]
    doc = _documented(name)
    assert doc, f"no documented values for {name}"
    for k, v in doc.items():
        if k in ("max_allocation_pct", "daily_loss_limit"):
            assert cls.risk_defaults.get(k) == v, (name, k)
        else:
            assert k in cls.default_params, (name, k)
            assert cls.default_params[k] == v, (name, k, cls.default_params[k], v)
