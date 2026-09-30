"""External data feeds used by strategies (``ctx.feeds``), e.g. crypto spot prices.

:class:`FeedRegistry` is a read-only mapping ``name -> feed`` that also allows attribute
access (``ctx.feeds.crypto`` == ``ctx.feeds["crypto"]``). Feeds are lazy: they make no
network request until a strategy asks for data, so registering a feed is free.

Built-in feeds (:func:`build_feeds`):

* ``crypto``: :class:`~kalshibot.feeds.crypto.CryptoSpotFeed` (Coinbase Exchange public
  API with Kraken fallback; spot + 1-minute candles, TTL-cached). Optional config::

      feeds:
        crypto: {symbols: [BTC, ETH], ttl_s: 5, candle_ttl_s: 30, max_rps: 3}

* ``kalshi_settled``: :class:`~kalshibot.feeds.kalshi_settled.KalshiSettledFeed` (recently
  finalized markets of a series from Kalshi's public REST API, TTL-cached; used e.g. for the
  BTC15M settlement basis). It uses ``kalshi.base_url``; pass ``kalshi_client=`` to
  :func:`build_feeds` to share the engine's client and rate limiter, otherwise it makes its own
  client with a small budget. Optional config::

      feeds:
        kalshi_settled: {ttl_s: 60, max_rps: 0.5}

Backtests register :mod:`kalshibot.feeds.replay` (``ReplayCryptoFeed``,
``ReplaySettledFeed``) under the same names: same methods, no look-ahead.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from kalshibot.feeds.crypto import CryptoSpotFeed, FeedError, SpotCandle, SpotQuote
from kalshibot.feeds.kalshi_settled import KalshiSettledFeed
from kalshibot.feeds.replay import ReplayCryptoFeed, ReplaySettledFeed

__all__ = [
    "CryptoSpotFeed",
    "FeedError",
    "FeedRegistry",
    "KalshiSettledFeed",
    "ReplayCryptoFeed",
    "ReplaySettledFeed",
    "SpotCandle",
    "SpotQuote",
    "build_feeds",
]

log = logging.getLogger(__name__)


class FeedRegistry(Mapping[str, Any]):
    """Named external data feeds (mapping + attribute access)."""

    def __init__(self, feeds: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_feeds", dict(feeds or {}))

    def register(self, name: str, feed: Any) -> Any:
        if not name or not name.isidentifier():
            raise ValueError(f"feed name must be an identifier, got {name!r}")
        self._feeds[name] = feed
        return feed

    def unregister(self, name: str) -> None:
        self._feeds.pop(name, None)

    # Mapping
    def __getitem__(self, name: str) -> Any:
        return self._feeds[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._feeds)

    def __len__(self) -> int:
        return len(self._feeds)

    def __getattr__(self, name: str) -> Any:
        feeds = self.__dict__.get("_feeds", {})
        if name in feeds:
            return feeds[name]
        raise AttributeError(f"no feed named {name!r} (available: {sorted(feeds)})")

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, feed in self._feeds.items():
            fn = getattr(feed, "status", None)
            try:
                out[name] = fn() if callable(fn) else {}
            except Exception as e:  # status must never raise
                out[name] = {"error": str(e)}
        return out

    async def aclose(self) -> None:
        for name, feed in self._feeds.items():
            fn = getattr(feed, "aclose", None)
            if not callable(fn):
                continue
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("closing feed %s failed", name)


def build_feeds(settings: Any = None, *, kalshi_client: Any = None, **overrides: Any) -> FeedRegistry:
    """The default registry (``crypto``, ``kalshi_settled``), configured from the optional ``feeds``
    config section. ``overrides`` go to the crypto feed; ``kalshi_client`` (optional) is shared by
    ``kalshi_settled``. No feed makes a request until a strategy asks."""
    section = getattr(settings, "feeds", None) if settings is not None else None
    if section is None and settings is not None:
        section = (getattr(settings, "model_extra", None) or {}).get("feeds")
    crypto_cfg: dict[str, Any] = {}
    settled_cfg: dict[str, Any] = {}
    if isinstance(section, Mapping):
        c = section.get("crypto")
        if isinstance(c, Mapping):
            crypto_cfg = {k: v for k, v in c.items()
                          if k in ("symbols", "ttl_s", "candle_ttl_s", "max_rps", "timeout", "sources")}
        k = section.get("kalshi_settled")
        if isinstance(k, Mapping):
            settled_cfg = {n: v for n, v in k.items() if n in ("ttl_s", "max_rps", "timeout")}
    crypto_cfg.update(overrides)
    kalshi = getattr(settings, "kalshi", None) if settings is not None else None
    base_url = getattr(kalshi, "base_url", None)
    if base_url:
        settled_cfg.setdefault("base_url", str(base_url))
    reg = FeedRegistry()
    reg.register("crypto", CryptoSpotFeed(**crypto_cfg))
    reg.register("kalshi_settled", KalshiSettledFeed(kalshi_client, **settled_cfg))
    return reg
