# -*- coding: utf-8 -*-
"""
ДАННЫЕ РЫНКА: откуда брать свечи и фьючерсные показатели, чтобы работало и на Mac,
и в облаке.

Binance не отдаёт данные серверам в США (ошибка 451), а серверы GitHub находятся там.
Поэтому:
  - спот (свечи, цена, стакан): в облаке — публичное зеркало рыночных данных
    Binance data-api.binance.vision (те же данные, без торговли); на Mac — обычный адрес;
  - фьючерсы (funding, открытый интерес, перекос лонг/шорт): пробуем биржи по очереди,
    берём первую, которая ответила. Если ни одна — анализ идёт без этих данных,
    в отчёте это видно.

Переопределить можно переменными окружения:
  SPOT_EXCHANGE   = binance (по умолчанию)
  SPOT_API_URL    = адрес публичного API спота (в облаке по умолчанию data-api.binance.vision)
  PERP_EXCHANGES  = binanceusdm,okx (порядок перебора)
"""

import os

IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
BINANCE_DATA_MIRROR = "https://data-api.binance.vision/api/v3"

_spot = None
_perp = None
_perp_tried = False
PERP_ERRORS = []


def spot():
    """Биржа для свечей, цен и стакана."""
    global _spot
    if _spot is None:
        import ccxt
        ex_id = os.environ.get("SPOT_EXCHANGE", "binance")
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
        url = os.environ.get("SPOT_API_URL") or (BINANCE_DATA_MIRROR if IN_CLOUD and ex_id == "binance" else None)
        if url and ex_id == "binance":
            ex.urls["api"]["public"] = url
            # только спот: фьючерсные адреса из облака заблокированы и сломали бы загрузку рынков
            ex.options["fetchMarkets"] = {"types": ["spot"]}
        _spot = ex
    return _spot


def perp():
    """Первая биржа с фьючерсными данными, которая отвечает. None, если ни одна."""
    global _perp, _perp_tried
    if _perp_tried:
        return _perp
    _perp_tried = True
    import ccxt
    default = "okx,binanceusdm" if IN_CLOUD else "binanceusdm,okx"
    for ex_id in os.environ.get("PERP_EXCHANGES", default).split(","):
        ex_id = ex_id.strip()
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
            if ex_id == "okx":
                ex.options["defaultType"] = "swap"
            ex.fetch_funding_rate(perp_symbol("BTC/USDT"))      # проверка, что отвечает
            _perp = ex
            return _perp
        except Exception as e:                       # noqa: BLE001
            PERP_ERRORS.append(f"{ex_id}: {str(e)[:120]}")
    return None


def perp_symbol(symbol):
    """BTC/USDT -> BTC/USDT:USDT (так ccxt называет бессрочный фьючерс)."""
    if ":" in symbol:
        return symbol
    return f"{symbol}:{symbol.split('/')[1]}"


def describe():
    p = perp()
    return {"spot": f"{spot().id} ({spot().urls['api'].get('public', '')})",
            "perp": p.id if p else "недоступны: " + "; ".join(PERP_ERRORS)}
