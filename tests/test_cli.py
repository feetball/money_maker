"""CLI: argument parsing, ``reset`` against a temp database, ``backtest`` without a runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kalshibot.cli import build_parser, main
from kalshibot.money import D
from kalshibot.risk import RiskManager
from kalshibot.store import Store


def write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"account: {{starting_balance: 500}}\nstorage: {{path: {tmp_path / 'db.sqlite3'}}}\n")
    return cfg


def test_parser() -> None:
    p = build_parser()
    a = p.parse_args(["serve", "--port", "9000", "--no-engine"])
    assert a.command == "serve" and a.port == 9000 and a.no_engine
    a = p.parse_args(["-c", "x.yaml", "reset", "--starting-balance", "250", "-y"])
    assert a.config == "x.yaml" and a.starting_balance == 250 and a.yes
    a = p.parse_args(["backtest", "--strategy", "s", "--params", '{"a": 1}'])
    assert a.strategy == "s" and a.params == '{"a": 1}'
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_reset_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path)
    store = Store(tmp_path / "db.sqlite3")
    store.save_account(starting_balance=D(500), cash=D(123))
    RiskManager(None, store=store).set_kill_switch(True, "test")
    store.close()
    assert main(["-c", str(cfg), "reset", "--yes", "--clear-kill-switch"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["starting_balance"] == 500 and out["cash"] == 500
    assert main(["-c", str(cfg), "reset", "-y", "--starting-balance", "42"]) == 0
    assert json.loads(capsys.readouterr().out)["cash"] == 42
    store = Store(tmp_path / "db.sqlite3")
    assert store.get_account()["cash"] == D(42)
    assert RiskManager(None, store=store).kill_switch is False
    store.close()


def test_reset_needs_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["-c", str(cfg), "reset"]) == 1


def test_missing_config_is_an_error(tmp_path: Path) -> None:
    assert main(["-c", str(tmp_path / "missing.yaml"), "reset", "-y"]) == 2


def test_backtest_without_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "kalshibot.backtest.runner", None)
    assert main(["-c", str(write_config(tmp_path)), "backtest", "--strategy", "x"]) == 2
    assert "unavailable" in capsys.readouterr().err
