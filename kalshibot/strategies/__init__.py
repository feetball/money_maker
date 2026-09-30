"""Strategy registry (ARCHITECTURE.md §7): ``REGISTRY: dict[str, type[Strategy]]``.

Strategies live one per module in this package (``kalshibot/strategies/<name>.py``). On
import, every submodule except ``base`` (and ``_private`` modules) is imported, and each
concrete :class:`Strategy` subclass **defined in that module** with a non-empty ``name``
is registered under that name. A module that fails to import is logged and recorded in
:data:`LOAD_ERRORS` and skipped, so one broken strategy never takes the app down.

Shipped strategies (research/FINDINGS.md; all four are on by default, see
``Strategy.enabled_by_default``): ``btc15m_favorite`` (primary), ``ladder_favorite`` and
``maker_favorite`` (experimental) and ``no_basket_arb``. Explicit registration also works (e.g.
for strategies defined elsewhere, or in tests)::

    from kalshibot.strategies import register

    @register
    class MyStrategy(Strategy):
        name = "my_strategy"
        ...
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import TypeVar

from kalshibot.strategies.base import (
    CancelIntent,
    OrderIntent,
    ParamError,
    Strategy,
    StrategyContext,
    UniverseSpec,
    coerce_params,
    intents_list,
)

__all__ = [
    "LOAD_ERRORS",
    "REGISTRY",
    "CancelIntent",
    "OrderIntent",
    "ParamError",
    "Strategy",
    "StrategyContext",
    "UniverseSpec",
    "coerce_params",
    "discover",
    "intents_list",
    "register",
    "unregister",
]

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Strategy]] = {}
#: ``{module_name: "ExcType: message"}`` for strategy modules that failed to import.
LOAD_ERRORS: dict[str, str] = {}

S = TypeVar("S", bound=type[Strategy])

_SKIP = frozenset({"base"})


def register(cls: S) -> S:
    """Class decorator: add ``cls`` to :data:`REGISTRY` under ``cls.name``."""
    if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
        raise TypeError(f"{cls!r} is not a Strategy subclass")
    if not cls.name:
        raise ValueError(f"{cls.__name__} has no `name`")
    prev = REGISTRY.get(cls.name)
    if prev is not None and prev is not cls:
        log.warning("strategy name %r registered twice (%s, %s); keeping the latter",
                    cls.name, prev.__qualname__, cls.__qualname__)
    REGISTRY[cls.name] = cls
    return cls


def unregister(name: str) -> None:
    """Remove ``name`` from the registry (tests)."""
    REGISTRY.pop(name, None)


def discover(package: str = __name__) -> dict[str, type[Strategy]]:
    """Import every strategy module of ``package`` and register the strategies it defines."""
    pkg = importlib.import_module(package)
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name in _SKIP or info.name.startswith("_"):
            continue
        modname = f"{package}.{info.name}"
        try:
            mod = importlib.import_module(modname)
        except Exception as e:  # a broken strategy must not break the app
            LOAD_ERRORS[modname] = f"{type(e).__name__}: {e}"
            log.exception("failed to import strategy module %s", modname)
            continue
        LOAD_ERRORS.pop(modname, None)
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == mod.__name__
                    and getattr(obj, "name", "") and not inspect.isabstract(obj)):
                register(obj)
    return REGISTRY


discover()
