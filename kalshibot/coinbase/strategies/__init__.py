"""Coinbase spot strategy registry (docs/COINBASE_CONTRACT.md §8): ``REGISTRY: dict[str, type[SpotStrategy]]``.

PAPER TRADING ONLY. Mirrors :mod:`kalshibot.strategies` but is a separate registry: Kalshi
and Coinbase strategies never share names, classes or state.

Strategies live one per module in this package (``kalshibot/coinbase/strategies/<name>.py``).
On import, every submodule except ``base`` (and ``_private`` modules) is imported, and each
concrete :class:`SpotStrategy` subclass **defined in that module** with a non-empty ``name``
is registered under that name. A module that fails to import, or a class with invalid class
attributes (:meth:`SpotStrategy.class_problems`), is logged and recorded in
:data:`LOAD_ERRORS` and skipped, so one broken strategy never takes the venue down.
Explicit registration also works (e.g. for strategies defined elsewhere, or in tests)::

    from kalshibot.coinbase.strategies import register

    @register
    class MySpotStrategy(SpotStrategy):
        name = "my_spot_strategy"
        ...
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import TypeVar

from kalshibot.coinbase.strategies.base import (
    EXECUTIONS,
    GRANULARITIES,
    ParamError,
    SpotContext,
    SpotStrategy,
    TargetWeight,
    allocation_pct,
    coerce_params,
    normalize_targets,
    resolve_enabled,
    resolve_strategy_params,
    strategy_config,
)

__all__ = [
    "EXECUTIONS",
    "GRANULARITIES",
    "LOAD_ERRORS",
    "REGISTRY",
    "ParamError",
    "SpotContext",
    "SpotStrategy",
    "TargetWeight",
    "allocation_pct",
    "coerce_params",
    "discover",
    "get_strategy",
    "normalize_targets",
    "register",
    "resolve_enabled",
    "resolve_strategy_params",
    "strategy_config",
    "unregister",
]

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[SpotStrategy]] = {}
#: ``{module_or_class: "ExcType: message"}`` for strategy modules/classes that failed to load.
LOAD_ERRORS: dict[str, str] = {}

S = TypeVar("S", bound=type[SpotStrategy])

_SKIP = frozenset({"base"})


def register(cls: S) -> S:
    """Class decorator: add ``cls`` to :data:`REGISTRY` under ``cls.name``.

    Raises ``TypeError`` for non-strategies and ``ValueError`` for invalid class attributes.
    """
    if not (inspect.isclass(cls) and issubclass(cls, SpotStrategy)):
        raise TypeError(f"{cls!r} is not a SpotStrategy subclass")
    bad = cls.class_problems()
    if bad:
        raise ValueError(f"{cls.__name__}: {'; '.join(bad)}")
    prev = REGISTRY.get(cls.name)
    if prev is not None and prev is not cls:
        log.warning("coinbase strategy name %r registered twice (%s, %s); keeping the latter",
                    cls.name, prev.__qualname__, cls.__qualname__)
    REGISTRY[cls.name] = cls
    return cls


def unregister(name: str) -> None:
    """Remove ``name`` from the registry (tests)."""
    REGISTRY.pop(name, None)


def get_strategy(name: str) -> type[SpotStrategy]:
    """The registered class for ``name``; ``KeyError`` listing the known names otherwise."""
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown coinbase strategy {name!r}; known: {known}") from None


def discover(package: str = __name__) -> dict[str, type[SpotStrategy]]:
    """Import every strategy module of ``package`` and register the strategies it defines."""
    pkg = importlib.import_module(package)
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name in _SKIP or info.name.startswith("_"):
            continue
        modname = f"{package}.{info.name}"
        try:
            mod = importlib.import_module(modname)
        except Exception as e:  # a broken strategy must not break the venue
            LOAD_ERRORS[modname] = f"{type(e).__name__}: {e}"
            log.exception("failed to import coinbase strategy module %s", modname)
            continue
        LOAD_ERRORS.pop(modname, None)
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if not (issubclass(obj, SpotStrategy) and obj is not SpotStrategy and obj.__module__ == mod.__name__
                    and bool(obj.name) and not inspect.isabstract(obj)):
                continue
            key = f"{modname}.{obj.__name__}"
            try:
                register(obj)
            except (TypeError, ValueError) as e:
                LOAD_ERRORS[key] = f"{type(e).__name__}: {e}"
                log.error("coinbase strategy %s not registered: %s", key, e)
            else:
                LOAD_ERRORS.pop(key, None)
    return REGISTRY


discover()
