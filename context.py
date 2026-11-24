# -*- coding: utf-8 -*-
"""
КОНТЕКСТ: что агенты знают о портфеле и о рынке в целом.

  - portfolio_block : открытые позиции и последние закрытые сделки — в каждый промпт
  - market_regime   : режим рынка по BTC на дневных свечах (общее направление)
  - context_gates   : правила в КОДЕ поверх риск-движка:
        regime_ok          — не идём против режима BTC (урок трёх шортов в растущий рынок)
        with_trend         — не идём против тренда самой монеты на 4-часовых свечах
        same_direction_cap — не больше 3 позиций в одну сторону
        not_already_open   — второй позиции по монете не будет
  - check_trend_claim : если модель назвала тренд не так, как его считает код, верим коду
"""

import datetime

SAME_DIRECTION_CAP = 3
REGIME_SYMBOL = "BTC/USDT"

REGIME_RU = {"up": "восходящий", "down": "нисходящий", "range": "боковой"}
DIR_RU = {"long": "лонг", "short": "шорт"}


def _sma(vals, n):
    n = min(n, len(vals))
    return sum(vals[-n:]) / n if n else 0.0


def regime_from_closes(closes):
    """Режим по дневным закрытиям: цена и SMA20 относительно SMA50."""
    if len(closes) < 20:
        return "range"
    last, s20, s50 = closes[-1], _sma(closes, 20), _sma(closes, 50)
    if last > s50 and s20 > s50:
        return "up"
    if last < s50 and s20 < s50:
        return "down"
    return "range"


def market_regime(exchange, symbol=REGIME_SYMBOL):
    """Текущий режим рынка. При ошибке сети — regime=None (гейт тогда не блокирует)."""
    try:
        o = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=60)
        closes = [c[4] for c in o]
        r = regime_from_closes(closes)
        return {"regime": r, "symbol": symbol, "last": closes[-1],
                "sma20_1d": round(_sma(closes, 20), 2), "sma50_1d": round(_sma(closes, 50), 2),
                "note": f"режим рынка по {symbol} на дневных свечах: {REGIME_RU[r]}"}
    except Exception as e:                           # noqa: BLE001
        return {"regime": None, "symbol": symbol, "note": f"режим рынка неизвестен ({e})"}


def regime_at(exchange, when, symbol=REGIME_SYMBOL):
    """Режим рынка на заданный момент в прошлом (для разбора старых сделок)."""
    since = int((when - datetime.timedelta(days=75)).timestamp() * 1000)
    o = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since, limit=80)
    cutoff = when.timestamp() * 1000
    closes = [c[4] for c in o if c[0] < cutoff]
    return regime_from_closes(closes)


def portfolio_block(portfolio, results, last_n=5):
    """Короткая сводка портфеля для промптов: что открыто и чем кончились последние сделки."""
    now = datetime.datetime.now()
    opened = []
    for p in portfolio.get("open_positions", []):
        try:
            days = (now - datetime.datetime.fromisoformat(p["opened_at"])).days
        except (KeyError, ValueError):
            days = None
        opened.append({"instrument": p.get("instrument"), "direction": p.get("direction"),
                       "entry": p.get("entry"), "stop": p.get("stop"),
                       "target": p.get("target_far") or p.get("targets"),
                       "risk_pct": p.get("risk_pct"), "days_open": days})
    recent = [{"instrument": r.get("instrument"), "direction": r.get("direction"),
               "hit": r.get("hit"), "actual_R": r.get("actual_R")}
              for r in results[-last_n:]]
    return {"open_positions": opened,
            "heat_pct": round(sum(p.get("risk_pct", 0) or 0 for p in portfolio.get("open_positions", [])), 3),
            "recent_closed": recent}


def context_gates(symbol, direction, md, regime, open_positions):
    """Дополнительные гейты. Возвращает (gates, reasons)."""
    reg = (regime or {}).get("regime")
    trend = (md or {}).get("trend")
    same = sum(1 for p in open_positions if p.get("direction") == direction)
    bounce = is_bounce(direction, md, regime)
    gates = {
        "regime_ok": bounce or not ((reg == "up" and direction == "short") or (reg == "down" and direction == "long")),
        "with_trend": not ((trend == "up" and direction == "short") or (trend == "down" and direction == "long")),
        "same_direction_cap": same < SAME_DIRECTION_CAP,
        "not_already_open": symbol not in {p.get("instrument") for p in open_positions},
    }
    if bounce:
        gates["bounce_cap"] = bounce_open(open_positions) < BOUNCE_CAP
    reasons = []
    if not gates.get("bounce_cap", True):
        reasons.append(f"уже {BOUNCE_CAP} позиции в режиме «отскок» — лимит")
    if not gates["regime_ok"]:
        reasons.append(f"{DIR_RU.get(direction, direction)} против режима рынка "
                       f"(BTC {REGIME_RU.get(reg, reg)})")
    if not gates["with_trend"]:
        reasons.append(f"{DIR_RU.get(direction, direction)} против тренда монеты на 4ч "
                       f"({REGIME_RU.get(trend, trend)})")
    if not gates["same_direction_cap"]:
        reasons.append(f"уже {same} позиции в сторону «{DIR_RU.get(direction, direction)}», "
                       f"лимит {SAME_DIRECTION_CAP}")
    if not gates["not_already_open"]:
        reasons.append(f"по {symbol} уже есть открытая позиция")
    return gates, reasons


_REGIME_WORDS = {"up": ("up", "uptrend", "bull", "восход", "рост"),
                 "down": ("down", "downtrend", "bear", "нисход", "паден"),
                 "range": ("range", "flat", "sideways", "боков", "флэт")}


def check_trend_claim(tech, md):
    """Если модель назвала режим монеты иначе, чем его считает код, верим коду.
    Возвращает текст пометки или None."""
    claim = str(tech.get("regime", "")).lower()
    code = (md or {}).get("trend")
    if not claim or not code:
        return None
    said = next((k for k, words in _REGIME_WORDS.items() if any(w in claim for w in words)), None)
    if said and said != code:
        tech["regime_model"] = tech.get("regime")
        tech["regime"] = code
        return (f"аналитик назвал тренд «{tech['regime_model']}», а по скользящим средним он "
                f"{REGIME_RU.get(code, code)} — принят расчёт кода")
    return None


# ── режим «отскок» ──────────────────────────────────────────────────────────
# Монета идёт ПО СВОЕМУ тренду против рынка: альт растёт, когда BTC падает (лонг),
# или падает, когда BTC растёт (шорт). Сделку разрешаем, но осторожно:
# половинный риск и не больше BOUNCE_CAP таких позиций сразу.
BOUNCE_RISK_PCT = 0.5
BOUNCE_CAP = 2
BOUNCE_NOTE = ("режим «отскок»: монета идёт по своему тренду против рынка (BTC). "
               "Риск — половина обычного, таких позиций не больше 2. Нужен ясный сетап "
               "по тренду монеты с близким уровнем для стопа; любая слабость — no_trade.")


def is_bounce(direction, md, regime):
    reg = (regime or {}).get("regime"); trend = (md or {}).get("trend")
    return (reg == "down" and trend == "up" and direction == "long") or \
           (reg == "up" and trend == "down" and direction == "short")


def bounce_open(open_positions):
    return sum(1 for p in open_positions if p.get("mode") == "bounce")


def allowed_directions(md, regime):
    """Какие направления сделки вообще пропустят гейты — считаем ДО вызова модели.
    Возвращает (список направлений, причина отсева или None)."""
    trend = (md or {}).get("trend")
    reg = (regime or {}).get("regime")
    if md.get("ohlcv_error"):
        return [], f"нет данных свечей ({md['ohlcv_error'][:80]})"
    if trend == "range":
        return [], "тренд монеты на 4ч боковой — сетап по тренду невозможен"
    by_trend = {"up": ["long"], "down": ["short"]}.get(trend, ["long", "short"])
    allowed = list(by_trend)
    if reg == "up":
        allowed = [d for d in allowed if d == "long"]
    elif reg == "down":
        allowed = [d for d in allowed if d == "short"]
    if not allowed and trend in ("up", "down"):
        return by_trend, None                 # режим «отскок» — проверим гейтами и половинным риском
    if not allowed:
        return [], (f"тренд монеты {REGIME_RU.get(trend, trend)}, а режим рынка BTC "
                    f"{REGIME_RU.get(reg, reg)} — разрешённых направлений нет")
    return allowed, None
