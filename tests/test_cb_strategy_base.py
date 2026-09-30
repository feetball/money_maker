"""Coinbase spot strategy interface: TargetWeight validation, SpotStrategy, registry, resolution helpers."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import pytest

from kalshibot.coinbase.config import CoinbaseSettings, CoinbaseStrategySettings
from kalshibot.coinbase.strategies import (
    LOAD_ERRORS,
    REGISTRY,
    ParamError,
    SpotStrategy,
    TargetWeight,
    allocation_pct,
    discover,
    get_strategy,
    normalize_targets,
    register,
    resolve_enabled,
    resolve_strategy_params,
    strategy_config,
    unregister,
)


class _Demo(SpotStrategy):
    name = "cb_test_demo"
    description = "demo"
    default_params = {"lookback": 20, "products": ["BTC-USD", "ETH-USD"], "flag": False}
    param_schema = {
        "lookback": {"type": "int", "min": 2, "max": 500},
        "products": {"type": "list"},
        "flag": {"type": "bool"},
    }
    risk_defaults = {"max_allocation_pct": 30}

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        return None


# --------------------------------------------------------------------------- TargetWeight


def test_target_weight_normalizes_fields() -> None:
    tw = TargetWeight(" btc-usd ", "0.25", None, expected_edge_bps="12.5", score=float("inf"))  # type: ignore[arg-type]
    assert tw.product_id == "BTC-USD"
    assert tw.weight == 0.25
    assert tw.reason == ""
    assert tw.expected_edge_bps == 12.5
    assert tw.score is None
    assert tw.problems() == []
    assert tw.to_json() == {"product_id": "BTC-USD", "weight": 0.25, "reason": "", "expected_edge_bps": 12.5,
                            "score": None}


@pytest.mark.parametrize("weight, fragment", [("x", "finite"), (float("nan"), "finite"), (-0.1, "no shorting"),
                                              (1.5, "no leverage"), (True, "finite")])
def test_target_weight_problems(weight: Any, fragment: str) -> None:
    tw = TargetWeight("ETH-USD", weight)
    assert any(fragment in p for p in tw.problems())


def test_normalize_targets_none_empty_and_mapping() -> None:
    assert normalize_targets(None) == (None, [])
    assert normalize_targets([]) == ([], [])
    out, problems = normalize_targets({"eth-usd": 0.3, "BTC-USD": TargetWeight("BTC-USD", 0.5, "trend")})
    assert problems == []
    assert [(t.product_id, t.weight, t.reason) for t in out] == [("BTC-USD", 0.5, "trend"), ("ETH-USD", 0.3, "")]
    single, _ = normalize_targets(TargetWeight("SOL-USD", 0.1))
    assert [t.product_id for t in single] == ["SOL-USD"]


def test_normalize_targets_corrections() -> None:
    src = [
        TargetWeight("BTC-USD", 0.9),
        TargetWeight("ETH-USD", -0.2),  # clamped to 0
        TargetWeight("SOL-USD", 0.3),
        TargetWeight("SOL-USD", 0.6),  # duplicate: the last wins
        TargetWeight("XYZ-USD", 0.1),  # not available
        TargetWeight("DOGE-USD", float("nan")),  # dropped
        "junk",
    ]
    out, problems = normalize_targets(src, {"BTC-USD": 1, "ETH-USD": 1, "SOL-USD": 1, "DOGE-USD": 1})
    assert [t.product_id for t in out] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    w = {t.product_id: t.weight for t in out}
    assert w["ETH-USD"] == 0.0
    assert math.isclose(w["BTC-USD"] + w["SOL-USD"], 1.0)  # 0.9 + 0.6 scaled down to 1
    assert math.isclose(w["BTC-USD"] / w["SOL-USD"], 0.9 / 0.6)
    text = " | ".join(problems)
    for frag in ("no shorting", "duplicate", "not an available product", "finite", "ignored str", "scaled down"):
        assert frag in text
    assert src[0].weight == 0.9  # inputs are not mutated


def test_normalize_targets_rejects_non_list() -> None:
    out, problems = normalize_targets(42)
    assert out == [] and "expected a list" in problems[0]


def test_normalize_targets_sum_tolerance() -> None:
    out, problems = normalize_targets([TargetWeight("A-USD", 0.1)] * 1 + [TargetWeight(f"P{i}-USD", 0.1)
                                                                        for i in range(9)])
    assert problems == []  # 10 x 0.1 == 1 within float noise
    assert len(out) == 10


# --------------------------------------------------------------------------- SpotStrategy


def test_spot_strategy_params_and_defaults() -> None:
    s = _Demo({"lookback": "30", "flag": "yes"})
    assert s.params == {"lookback": 30, "products": ["BTC-USD", "ETH-USD"], "flag": True}
    assert _Demo.resolve_params({"nope": 1}) == _Demo.default_params  # non-strict drops unknown names
    with pytest.raises(ParamError, match="unknown parameter"):
        _Demo.resolve_params({"nope": 1}, strict=True)
    with pytest.raises(ParamError, match="below the minimum"):
        _Demo({"lookback": 1})
    # class-level defaults of the contract
    assert (_Demo.bar_granularity_s, _Demo.history_bars, _Demo.execution, _Demo.rebalance_band,
            _Demo.backtestable, _Demo.experimental, _Demo.enabled_by_default) == (86400, 250, "taker", 0.02,
                                                                                True, False, False)


def test_default_universe_uses_products_param() -> None:
    products = {"BTC-USD": object(), "SOL-USD": object()}
    assert _Demo().universe(products) == ["BTC-USD"]  # ETH-USD is not available
    assert _Demo({"products": "sol-usd, btc-usd, SOL-USD"}).universe(products) == ["SOL-USD", "BTC-USD"]

    class NoParam(SpotStrategy):
        name = "cb_test_noparam"

        def on_bar(self, ctx: Any) -> None:
            return None

    assert NoParam().universe(products) == ["BTC-USD"]
    assert NoParam().universe({}) == []


def test_abstract_on_bar_required() -> None:
    class Incomplete(SpotStrategy):
        name = "cb_test_incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_class_problems_and_info() -> None:
    assert _Demo.class_problems() == []

    class Bad(_Demo):
        name = ""
        bar_granularity_s = 900
        execution = "yolo"
        history_bars = 0
        rebalance_band = 1.5

    probs = " | ".join(Bad.class_problems())
    for frag in ("missing name", "bar_granularity_s", "execution", "history_bars", "rebalance_band"):
        assert frag in probs
    info = _Demo({"lookback": 25}).info()
    assert info["name"] == "cb_test_demo" and info["params"]["lookback"] == 25
    assert info["param_schema"]["lookback"]["default"] == 20
    assert info["bar_granularity_s"] == 86400 and info["execution"] == "taker"
    assert _Demo().dump_state() is None
    _Demo().load_state({"x": 1})  # optional hook: a no-op


# --------------------------------------------------------------------------- registry


def test_register_and_unregister() -> None:
    try:
        assert register(_Demo) is _Demo
        assert REGISTRY["cb_test_demo"] is _Demo
        assert get_strategy("cb_test_demo") is _Demo
    finally:
        unregister("cb_test_demo")
    assert "cb_test_demo" not in REGISTRY
    with pytest.raises(KeyError, match="unknown coinbase strategy"):
        get_strategy("cb_test_demo")
    with pytest.raises(TypeError):
        register(int)  # type: ignore[type-var]

    class Bad(_Demo):
        name = "cb_test_bad"
        bar_granularity_s = 60

    with pytest.raises(ValueError, match="bar_granularity_s"):
        register(Bad)


def test_discover_temp_package(tmp_path: Path) -> None:
    pkg = tmp_path / "cb_tmpstrats_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(
        "from kalshibot.coinbase.strategies.base import SpotStrategy, TargetWeight\n"
        "from kalshibot.coinbase.strategies.base import SpotStrategy as Imported\n"
        "class Good(SpotStrategy):\n"
        "    name = 'cb_tmp_good'\n"
        "    def on_bar(self, ctx):\n"
        "        return None\n"
        "class _Abstract(SpotStrategy):\n"
        "    name = 'cb_tmp_abstract'\n"
        "class BadGranularity(SpotStrategy):\n"
        "    name = 'cb_tmp_badgran'\n"
        "    bar_granularity_s = 60\n"
        "    def on_bar(self, ctx):\n"
        "        return None\n")
    (pkg / "broken.py").write_text("raise ImportError('boom')\n")
    (pkg / "_private.py").write_text("raise RuntimeError('must not be imported')\n")
    sys.path.insert(0, str(tmp_path))
    try:
        discover("cb_tmpstrats_pkg")
        assert "cb_tmp_good" in REGISTRY
        assert "cb_tmp_abstract" not in REGISTRY
        assert "cb_tmp_badgran" not in REGISTRY
        assert "cb_tmpstrats_pkg.broken" in LOAD_ERRORS
        assert "bar_granularity_s" in LOAD_ERRORS["cb_tmpstrats_pkg.good.BadGranularity"]
        assert not any("_private" in k for k in LOAD_ERRORS)
    finally:
        sys.path.remove(str(tmp_path))
        for k in [k for k in LOAD_ERRORS if k.startswith("cb_tmpstrats_pkg")]:
            LOAD_ERRORS.pop(k, None)
        unregister("cb_tmp_good")
        for m in [m for m in sys.modules if m.startswith("cb_tmpstrats_pkg")]:
            sys.modules.pop(m, None)


def test_real_registry_is_separate_from_kalshi() -> None:
    from kalshibot.strategies import REGISTRY as KALSHI_REGISTRY

    assert REGISTRY is not KALSHI_REGISTRY
    for name, cls in REGISTRY.items():
        assert issubclass(cls, SpotStrategy) and cls.name == name
        assert cls.class_problems() == []
        assert name not in KALSHI_REGISTRY


# --------------------------------------------------------------------------- resolution helpers


def test_resolve_enabled_precedence() -> None:
    assert resolve_enabled(_Demo) == (False, "default")
    assert resolve_enabled(_Demo, config=True) == (True, "config")
    assert resolve_enabled(_Demo, stored=False, config=True) == (False, "dashboard")

    class On(_Demo):
        enabled_by_default = True

    assert resolve_enabled(On) == (True, "default")
    assert resolve_enabled(On, config=False) == (False, "config")


def test_resolve_strategy_params() -> None:
    params, err = resolve_strategy_params(_Demo, config_params={"lookback": 40, "flag": True},
                                          overrides={"lookback": 50})
    assert err is None and params["lookback"] == 50 and params["flag"] is True
    params, err = resolve_strategy_params(_Demo, config_params={"lookback": 1})
    assert params == _Demo.default_params and "minimum" in (err or "")


def test_strategy_config_and_allocation() -> None:
    cb = CoinbaseSettings(strategies={"cb_test_demo": CoinbaseStrategySettings(enabled=True,
                                                                               params={"lookback": 60},
                                                                               max_allocation_pct=25)})
    assert strategy_config(cb, "cb_test_demo") == (True, {"lookback": 60})
    assert strategy_config(cb, "other") == (None, {})
    assert allocation_pct(cb, "cb_test_demo", _Demo) == 25.0
    assert allocation_pct(cb, "other", _Demo) == 30.0  # class risk_defaults
    assert allocation_pct(cb, "other") == 50.0  # coinbase.risk.max_strategy_allocation_pct

    from kalshibot.config import Settings

    s = Settings()
    s.coinbase = cb
    assert strategy_config(s, "cb_test_demo") == (True, {"lookback": 60})
    plain = {"coinbase": {"strategies": {"x": {"enabled": False, "params": {"a": 1}}}}}
    assert strategy_config(plain, "x") == (False, {"a": 1})
    assert strategy_config(None, "x") == (None, {})
    assert allocation_pct(None, "x") is None
