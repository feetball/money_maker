"""Strategy interface: OrderIntent normalization, parameter validation, registry discovery."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import DummyStrategy

from kalshibot.strategies import LOAD_ERRORS, REGISTRY, discover, register, unregister
from kalshibot.strategies.base import (
    OrderIntent,
    ParamError,
    Strategy,
    UniverseSpec,
    coerce_params,
    intents_list,
)


def test_order_intent_defaults_and_normalization() -> None:
    i = OrderIntent(ticker="KX-1", side="YES", limit_price=0.43, expected_edge=0.021, fair_value="0.6")
    assert i.action == "buy" and i.count == 1 and i.tif == "ioc" and i.expires_in_s is None
    assert i.side == "yes"
    assert i.limit_price == Decimal("0.43") and isinstance(i.limit_price, Decimal)
    assert i.expected_edge == Decimal("0.021")
    assert i.fair_value == 0.6
    assert i.problems() == []
    assert i.buy_side == "yes" and i.buy_price == Decimal("0.43")
    s = OrderIntent("KX-1", "yes", "sell", 3.0, limit_price=Decimal("0.70"))
    assert s.count == 3 and s.buy_side == "no" and s.buy_price == Decimal("0.30")
    assert s.to_json()["limit_price"] == 0.7


def test_order_intent_limit_price_is_required() -> None:
    with pytest.raises(TypeError):
        OrderIntent(ticker="KX-1", side="yes")  # type: ignore[call-arg]


@pytest.mark.parametrize(("kw", "needle"), [
    ({"side": "maybe"}, "side"),
    ({"action": "hold"}, "action"),
    ({"tif": "fok"}, "tif"),
    ({"count": 0}, "count"),
    ({"count": 2.5}, "count"),
    ({"limit_price": 1}, "limit_price"),
    ({"limit_price": "abc"}, "limit_price"),
    ({"ticker": ""}, "ticker"),
])
def test_order_intent_problems(kw: dict[str, Any], needle: str) -> None:
    base: dict[str, Any] = {"ticker": "KX-1", "side": "yes", "limit_price": Decimal("0.5")}
    base.update(kw)
    i = OrderIntent(**base)
    assert any(needle in p for p in i.problems())


def test_universe_spec() -> None:
    assert UniverseSpec().is_empty
    assert not UniverseSpec(max_days_to_close=1).is_empty
    assert not UniverseSpec(series_tickers=["KXBTCD"]).is_empty
    assert UniverseSpec(max_days_to_close=0).is_empty


def test_coerce_params() -> None:
    schema = DummyStrategy.param_schema
    out = coerce_params(schema, {"max_price": "0.5", "count": 3.0, "tif": "gtc", "series": "A, B"})
    assert out == {"max_price": 0.5, "count": 3, "tif": "gtc", "series": ["A", "B"]}
    with pytest.raises(ParamError, match="unknown parameter"):
        coerce_params(schema, {"nope": 1})
    assert coerce_params(schema, {"nope": 1}, strict=False) == {}
    with pytest.raises(ParamError, match="above the maximum"):
        coerce_params(schema, {"count": 101})
    with pytest.raises(ParamError, match="below the minimum"):
        coerce_params(schema, {"max_price": 0})
    with pytest.raises(ParamError, match="integer"):
        coerce_params(schema, {"count": 1.5})
    with pytest.raises(ParamError, match="not one of"):
        coerce_params(schema, {"tif": "fok"})
    with pytest.raises(ParamError, match="boolean"):
        coerce_params({"x": {"type": "bool"}}, {"x": "maybe"})
    assert coerce_params({"x": {"type": "bool"}}, {"x": "false"}) == {"x": False}
    assert coerce_params({}, {"anything": 1}) == {"anything": 1}  # no schema: pass-through


def test_strategy_params_merge_and_schema_json() -> None:
    s = DummyStrategy({"count": 5})
    assert s.params["count"] == 5 and s.params["max_price"] == 0.6
    assert DummyStrategy.resolve_params({"bogus": 1}) == DummyStrategy.default_params  # lenient by default
    with pytest.raises(ParamError):
        DummyStrategy.resolve_params({"bogus": 1}, strict=True)
    sj = DummyStrategy.schema_json()
    assert sj["count"]["default"] == 2 and sj["count"]["type"] == "int"
    assert Strategy.universe(s) == UniverseSpec()


def test_intents_list() -> None:
    i = OrderIntent(ticker="KX-1", side="yes", limit_price=Decimal("0.5"))
    assert intents_list(None) == []
    assert intents_list(i) == [i]
    assert intents_list((i, i)) == [i, i]


def test_register_and_discover(tmp_path: Path) -> None:
    pkg = tmp_path / "tmpstrats_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(
        "from kalshibot.strategies.base import Strategy\n"
        "class Good(Strategy):\n"
        "    name = 'tmp_good'\n"
        "    async def on_tick(self, ctx):\n"
        "        return []\n"
        "class _Abstract(Strategy):\n"
        "    name = 'tmp_abstract'\n")
    (pkg / "broken.py").write_text("raise ImportError('boom')\n")
    sys.path.insert(0, str(tmp_path))
    try:
        discover("tmpstrats_pkg")
        assert "tmp_good" in REGISTRY
        assert "tmp_abstract" not in REGISTRY  # abstract (no on_tick) is skipped
        assert "tmpstrats_pkg.broken" in LOAD_ERRORS
    finally:
        sys.path.remove(str(tmp_path))
        unregister("tmp_good")
        LOAD_ERRORS.pop("tmpstrats_pkg.broken", None)

    @register
    class Local(DummyStrategy):
        name = "tmp_local"

    try:
        assert REGISTRY["tmp_local"] is Local
    finally:
        unregister("tmp_local")
    with pytest.raises(TypeError):
        register(object)  # type: ignore[arg-type]
