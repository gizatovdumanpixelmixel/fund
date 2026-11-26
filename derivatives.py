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


def crowd_details(direction, pos, notional_usd=None):
    """Что именно перегрето — с цифрами и порогами. Список строк (пустой — толпа не против нас)."""
    if not pos or pos.get("crowded_side") != direction:
        return []
    f, ls, oi = pos.get("funding_rate"), pos.get("long_short_ratio"), pos.get("oi_change_24h_pct")
    out = []
    if direction == "long":
        if f is not None and f >= FUNDING_HOT:
            line = (f"финансирование +{f * 100:.3f}% за 8 ч (порог перегрева +{FUNDING_HOT * 100:.2f}%): "
                    f"лонги платят шортам, желающих купить с плечом слишком много")
            if notional_usd:
                line += f"; удержание твоей позиции стоит ≈${notional_usd * f * 3:.2f} в сутки"
            out.append(line)
        if ls is not None and ls >= LS_HOT:
            out.append(f"соотношение лонг/шорт {ls} (порог {LS_HOT}): на один счёт в шорте "
                       f"{ls} счёта в лонге")
    else:
        if f is not None and f <= FUNDING_COLD:
            line = (f"финансирование {f * 100:.3f}% за 8 ч (порог {FUNDING_COLD * 100:.2f}%): "
                    f"шорты платят лонгам, продавцов с плечом слишком много")
            if notional_usd:
                line += f"; удержание твоей позиции стоит ≈${notional_usd * abs(f) * 3:.2f} в сутки"
            out.append(line)
        if ls is not None and ls <= LS_COLD:
            out.append(f"соотношение лонг/шорт {ls} (порог {LS_COLD}): шортистов заметно больше")
    if out and oi is not None and oi >= OI_JUMP_PCT:
        out.append(f"открытый интерес вырос на {oi}% за сутки (порог {OI_JUMP_PCT}%) — "
                   f"плеча в монете становится больше, давление растёт")
    return out


def warning_for(direction, pos, notional_usd=None):
    """Текст предупреждения, если сделка идёт вместе с толпой. Иначе None."""
    det = crowd_details(direction, pos, notional_usd)
    if not det:
        return None
    what = ("риск резкого падения: при небольшом снижении чужие лонги начнут закрываться "
            "принудительно и толкать цену ниже" if direction == "long" else
            "риск шорт-сквиза: при небольшом росте чужие шорты начнут закрываться "
            "принудительно и толкать цену выше")
    return "толпа уже в " + ("лонгах" if direction == "long" else "шортах") + ": " + "; ".join(det) + " — " + what
