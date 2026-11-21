# -*- coding: utf-8 -*-
"""
ПОЗИЦИОНИРОВАНИЕ НА ФЬЮЧЕРСАХ: куда уже поставила толпа.

  - funding_rate         — плата за удержание: >0 — платят лонги (их больше), <0 — шорты
  - oi_change_24h_pct    — как изменился открытый интерес (сумма открытых позиций) за сутки
  - long_short_ratio     — сколько счетов в лонге на один счёт в шорте

Если толпа уже стоит в ту же сторону, что и наша сделка, рынок склонен к резкому
движению против неё: принудительные закрытия чужих позиций («сквиз»).
Пока это ПРЕДУПРЕЖДЕНИЕ, а не запрет: сделать его жёстким правилом решим на ретро,
когда наберётся статистика.

Данные — публичные, Binance USDⓈ-M фьючерсы, без ключей. Ошибка источника
не ломает анализ: поле просто остаётся пустым.
"""

FUNDING_HOT = 0.0005        # 0.05% за 8 часов — лонги перегреты
FUNDING_COLD = -0.0003      # -0.03% — перегреты шорты
LS_HOT = 2.0                # в лонге в 2+ раза больше счетов
LS_COLD = 0.7               # в шорте заметно больше
OI_JUMP_PCT = 15.0          # резкий рост открытого интереса за сутки


def perp_symbol(symbol):
    """BTC/USDT -> BTC/USDT:USDT (так ccxt называет бессрочный фьючерс)."""
    if ":" in symbol:
        return symbol
    quote = symbol.split("/")[1]
    return f"{symbol}:{quote}"


def _oi_change(perp, sym):
    hist = perp.fetch_open_interest_history(sym, timeframe="4h", limit=7)
    vals = [h.get("openInterestAmount") or h.get("openInterestValue") for h in hist]
    vals = [v for v in vals if v]
    if len(vals) < 2:
        return None
    return round((vals[-1] / vals[0] - 1) * 100, 2)


def _long_short(perp, sym):
    fn = getattr(perp, "fetch_long_short_ratio_history", None)
    if fn:
        hist = fn(sym, "4h", None, 2)
        if hist:
            return round(float(hist[-1]["longShortRatio"]), 2)
    raw = getattr(perp, "fapiDataGetGlobalLongShortAccountRatio", None)
    if raw:
        rows = raw({"symbol": sym.split(":")[0].replace("/", ""), "period": "4h", "limit": 2})
        if rows:
            return round(float(rows[-1]["longShortRatio"]), 2)
    return None


def crowding(funding, ls_ratio):
    """Где толпа: 'long', 'short' или None."""
    if (funding is not None and funding >= FUNDING_HOT) or (ls_ratio is not None and ls_ratio >= LS_HOT):
        return "long"
    if (funding is not None and funding <= FUNDING_COLD) or (ls_ratio is not None and ls_ratio <= LS_COLD):
        return "short"
    return None


def positioning(perp, symbol, funding=None):
    """Сводка позиционирования по монете. funding можно передать уже полученный."""
    out = {"funding_rate": funding, "oi_change_24h_pct": None, "long_short_ratio": None,
           "crowded_side": None, "errors": []}
    if perp is None:
        out["errors"].append("фьючерсные данные недоступны")
        return out
    sym = perp_symbol(symbol)
    if funding is None:
        try:
            out["funding_rate"] = perp.fetch_funding_rate(sym).get("fundingRate")
        except Exception as e:                       # noqa: BLE001
            out["errors"].append(f"funding: {e}")
    try:
        out["oi_change_24h_pct"] = _oi_change(perp, sym)
    except Exception as e:                           # noqa: BLE001
        out["errors"].append(f"открытый интерес: {e}")
    try:
        out["long_short_ratio"] = _long_short(perp, sym)
    except Exception as e:                           # noqa: BLE001
        out["errors"].append(f"лонг/шорт: {e}")
    out["crowded_side"] = crowding(out["funding_rate"], out["long_short_ratio"])
    return out


def warning_for(direction, pos):
    """Текст предупреждения, если сделка идёт вместе с толпой. Иначе None."""
    if not pos:
        return None
    side = pos.get("crowded_side")
    parts = []
    if side == direction == "long":
        parts.append("толпа уже в лонгах (высокий funding или перекос лонг/шорт) — "
                     "риск резкого падения из-за массового закрытия лонгов")
    elif side == direction == "short":
        parts.append("толпа уже в шортах — риск шорт-сквиза: резкого роста из-за "
                     "принудительного закрытия чужих шортов")
    oi = pos.get("oi_change_24h_pct")
    if parts and oi is not None and oi >= OI_JUMP_PCT:
        parts.append(f"открытый интерес вырос на {oi}% за сутки — давление растёт")
    return "; ".join(parts) if parts else None
