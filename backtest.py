# -*- coding: utf-8 -*-
"""
БЭКТЕСТ: зарабатывают ли правила системы на истории — без моделей, только код.

Что повторяет живую систему (pipeline.py / context.py):
  - анализ 2 раза в сутки (закрытые 4ч-свечи 00:00 и 12:00 UTC ≈ 05:00 и 17:00 по Алматы);
  - сводка как в _digest: тренд по SMA10/30 на 4ч, свинги за 20 свечей, диапазон за 80, средний размах 14;
  - режим рынка по BTC на дневных свечах (regime_from_closes), разрешённые направления как
    allowed_directions, режим «отскок» (половинный риск, не больше 2 таких позиций);
  - стоп за свингом, цели — ближайший и дальний уровень (swing_high_20 / range_high и зеркально),
    вход по закрытию свечи сигнала;
  - гейты: чистая R ≥ 1.5 с издержками (0.09% на сторону + финансирование лонга ~0.01%/8ч × 7 дней),
    стоп не уже 0.5 среднего размаха, одна позиция на монету, ≤3 в одну сторону, heat ≤ 4%,
    размер под 1% риска (0.5% «отскок») с потолком концентрации 15%;
  - выход: стоп или дальняя цель; если в одной свече оба — стоп; не решилось за 15 дней — по цене закрытия.

Чего НЕТ: рассуждений моделей, новостей, судьи и вето экспертов, дневного/спринтового стопа.
Поэтому это проверка ПРАВИЛ: если у них нет преимущества, модели сверху его не создадут.

Варианты (каждый — отдельный прогон портфеля):
  base       — как сейчас
  buf25      — стоп дальше свинга на 0.25 среднего размаха (запас против «выноса» стопов)
  no_bounce  — без режима «отскок»
  fixed      — цели не по уровням, а фиксированные: 1R и 2R от входа

Запуск: python backtest.py [--days 730] [--no-telegram]
"""

import argparse
import datetime
import json
import os
import sys

import context as C
import exits as EX

WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT",
             "ADA/USDT", "AVAX/USDT", "LINK/USDT", "TON/USDT", "TRX/USDT", "DOT/USDT",
             "LTC/USDT", "BCH/USDT", "NEAR/USDT", "SUI/USDT"]
TF_MS = 4 * 3600 * 1000
DAY_MS = 24 * 3600 * 1000
CAPITAL = 10000.0
RISK_PCT, BOUNCE_RISK_PCT, BOUNCE_CAP = 1.0, C.BOUNCE_RISK_PCT, C.BOUNCE_CAP
MAX_HEAT, MIN_R, MIN_STOP_VS_CANDLE = 4.0, 1.5, 0.5
CONC_CAP = 0.15 * CAPITAL
COST_SIDE = 0.0009                  # комиссия + спред + проскальзывание на сторону
FUNDING_8H = 0.0001                 # типичная ставка финансирования, платят лонги
HOLD_DAYS_ASSUMED, HORIZON_DAYS = 7, 15
SAME_DIR_CAP = C.SAME_DIRECTION_CAP
VARIANTS = {
    "base": {"buffer": 0.0, "bounce": True, "fixed": False},
    "buf25": {"buffer": 0.25, "bounce": True, "fixed": False},
    "no_bounce": {"buffer": 0.0, "bounce": False, "fixed": False},
    "fixed": {"buffer": 0.0, "bounce": True, "fixed": True},
}
NAMES = {"base": "как сейчас", "buf25": "стоп с запасом 0.25 размаха", "no_bounce": "без «отскока»",
         "fixed": "цели 1R и 2R вместо уровней"}
OUT_DIR = "backtest"


# ── данные ───────────────────────────────────────────────────────────────────

def fetch_all(ex, symbol, tf, since_ms):
    out, cursor = [], since_ms
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=tf, since=cursor, limit=1000)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 1
    seen, uniq = set(), []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0]); uniq.append(c)
    return uniq


def load_data(days):
    import market as MK
    ex = MK.spot()
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    since = now_ms - (days + 30) * DAY_MS              # +30 дней на разгон индикаторов
    data, errors = {}, []
    for s in WATCHLIST:
        try:
            c = fetch_all(ex, s, "4h", since)
            if len(c) > 120:
                data[s] = c
            print(f"  {s}: {len(c)} свечей 4ч")
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{s}: {e}")
    btc_daily = fetch_all(ex, "BTC/USDT", "1d", since - 80 * DAY_MS)
    return data, btc_daily, errors


# ── сводка и сетап (как в pipeline._digest и промпте теханалитика) ────────────

def digest(candles):
    closes = [c[4] for c in candles]
    last = closes[-1]
    fast, slow = C._sma(closes, 10), C._sma(closes, 30)
    trend = "up" if (fast > slow and last >= slow) else ("down" if (fast < slow and last <= slow) else "range")
    highs, lows = [c[2] for c in candles], [c[3] for c in candles]
    rng_hi, rng_lo = max(highs), min(lows)
    recent, last14 = candles[-20:], candles[-14:]
    return {"last": last, "trend": trend, "range_high": rng_hi, "range_low": rng_lo,
            "range_pos": (last - rng_lo) / (rng_hi - rng_lo) * 100 if rng_hi > rng_lo else 50.0,
            "swing_high": max(c[2] for c in recent), "swing_low": min(c[3] for c in recent),
            "avg_range": sum(c[2] - c[3] for c in last14) / len(last14)}


def regime_at(btc_daily, ts):
    closes = [c[4] for c in btc_daily if c[0] + DAY_MS <= ts][-60:]   # только закрытые дни
    return C.regime_from_closes(closes)


def allowed(md, reg, bounce_ok):
    """Как context.allowed_directions; возвращает [(направление, bounce)]."""
    trend = md["trend"]
    if trend == "range":
        return []
    by_trend = {"up": ["long"], "down": ["short"]}[trend]
    if reg == "up":
        ok = [d for d in by_trend if d == "long"]
    elif reg == "down":
        ok = [d for d in by_trend if d == "short"]
    else:
        ok = by_trend
    if ok:
        return [(d, False) for d in ok]
    return [(d, True) for d in by_trend] if bounce_ok else []


def build_setup(md, direction, v):
    """Сетап по правилам промпта теханалитика. None — сетапа нет."""
    entry, ar = md["last"], md["avg_range"]
    if direction == "long":
        if md["range_pos"] >= 90:                      # «цена не у самого range_high»
            return None
        stop = md["swing_low"] - v["buffer"] * ar
        unit = entry - stop
        if unit <= 0:
            return None
        if v["fixed"]:
            targets = [entry + unit, entry + 2 * unit]
        else:
            targets = sorted({t for t in (md["swing_high"], md["range_high"]) if t > entry * 1.001})
    else:
        if md["range_pos"] <= 10:
            return None
        stop = md["swing_high"] + v["buffer"] * ar
        unit = stop - entry
        if unit <= 0:
            return None
        if v["fixed"]:
            targets = [entry - unit, entry - 2 * unit]
        else:
            targets = sorted({t for t in (md["swing_low"], md["range_low"]) if t < entry * 0.999}, reverse=True)
    if not targets:
        return {"reject": "нет цели"}
    t_far = targets[-1]
    costs = (entry + t_far) * COST_SIDE
    if direction == "long":
        costs += entry * FUNDING_8H * 3 * HOLD_DAYS_ASSUMED
    r_net = (abs(t_far - entry) - costs) / unit
    if r_net < MIN_R:
        return {"reject": "R < 1.5"}
    if unit < MIN_STOP_VS_CANDLE * ar:
        return {"reject": "стоп слишком узкий"}
    return {"entry": entry, "stop": stop, "targets": targets, "t_far": t_far, "unit": unit, "R_net_plan": r_net}


def walk_plan(direction, entry, stop, t_far, path):
    """Выход по плану: (R до издержек финансирования, ts выхода, метка)."""
    for c in path:
        _ts, _o, h, l = c[0], c[1], c[2], c[3]
        hit_stop = l <= stop if direction == "long" else h >= stop
        hit_tgt = h >= t_far if direction == "long" else l <= t_far
        if hit_stop:
            return EX._r(direction, entry, stop, stop, COST_SIDE * 100), c[0] + TF_MS, "СТОП"
        if hit_tgt:
            return EX._r(direction, entry, stop, t_far, COST_SIDE * 100), c[0] + TF_MS, "ЦЕЛЬ"
    last = path[-1] if path else None
    if not last:
        return None
    return EX._r(direction, entry, stop, last[4], COST_SIDE * 100), last[0] + TF_MS, "СРОК"


# ── прогон портфеля ──────────────────────────────────────────────────────────

def run_variant(data, btc_daily, start_ms, v):
    idx = {s: {c[0]: i for i, c in enumerate(cs)} for s, cs in data.items()}
    steps = sorted({c[0] + TF_MS for cs in data.values() for c in cs
                    if (c[0] + TF_MS) % (12 * 3600 * 1000) == 0 and c[0] + TF_MS >= start_ms})
    open_pos, trades, rejects = [], [], {}
    for t in steps:
        open_pos = [p for p in open_pos if p["exit_ts"] > t]
        reg = regime_at(btc_daily, t)
        for s in WATCHLIST:
            cs = data.get(s)
            if not cs:
                continue
            i = idx[s].get(t - TF_MS)                  # свеча, закрывшаяся в момент t
            if i is None or i < 80:
                continue
            md = digest(cs[i - 79:i + 1])
            for direction, bounce in allowed(md, reg, v["bounce"]):
                st = build_setup(md, direction, v)
                if st is None:
                    continue
                if "reject" in st:
                    rejects[st["reject"]] = rejects.get(st["reject"], 0) + 1
                    continue
                if any(p["symbol"] == s for p in open_pos):
                    rejects["уже открыта"] = rejects.get("уже открыта", 0) + 1; continue
                if sum(p["direction"] == direction for p in open_pos) >= SAME_DIR_CAP:
                    rejects["лимит в одну сторону"] = rejects.get("лимит в одну сторону", 0) + 1; continue
                if bounce and sum(p["bounce"] for p in open_pos) >= BOUNCE_CAP:
                    rejects["лимит «отскоков»"] = rejects.get("лимит «отскоков»", 0) + 1; continue
                risk_target = (BOUNCE_RISK_PCT if bounce else RISK_PCT) / 100 * CAPITAL
                size = min(risk_target / st["unit"], CONC_CAP / st["entry"])
                risk_usd = size * st["unit"]
                heat = sum(p["risk_usd"] for p in open_pos) / CAPITAL * 100
                if heat + risk_usd / CAPITAL * 100 > MAX_HEAT + 1e-9:
                    rejects["heat"] = rejects.get("heat", 0) + 1; continue
                path = [c for c in cs[i + 1:] if c[0] < t + HORIZON_DAYS * DAY_MS]
                if not path:
                    continue
                walked = walk_plan(direction, st["entry"], st["stop"], st["t_far"], path)
                if not walked:
                    continue
                r_plan, exit_ts, label = walked
                held_days = (exit_ts - t) / DAY_MS
                fund_r = (st["entry"] * FUNDING_8H * 3 * held_days / st["unit"]) if direction == "long" else 0.0
                alt = EX.simulate(direction, st["entry"], st["stop"], st["targets"], path) or {"R": {}}
                tr = {"symbol": s, "direction": direction, "bounce": bounce, "regime": reg,
                      "opened": t, "exit_ts": exit_ts, "label": label, "held_days": round(held_days, 2),
                      "entry": st["entry"], "stop": st["stop"], "t1": st["targets"][0], "t_far": st["t_far"],
                      "R_net_plan": round(st["R_net_plan"], 2), "risk_usd": round(risk_usd, 2),
                      "R": round(r_plan - fund_r, 3),
                      "R_partial_t1": round(alt["R"].get("partial_t1", r_plan) - fund_r, 3),
                      "R_t1_all": round(alt["R"].get("t1_all", r_plan) - fund_r, 3)}
                trades.append(tr)
                open_pos.append({"symbol": s, "direction": direction, "bounce": bounce,
                                 "risk_usd": risk_usd, "exit_ts": exit_ts})
    return trades, rejects


# ── статистика ───────────────────────────────────────────────────────────────

def stats(trades, key="R"):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t[key] for t in trades]
    wins = [r for r in rs if r > 0]
    gross_win, gross_loss = sum(wins), -sum(r for r in rs if r <= 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        eq += t[key]; peak = max(peak, eq); dd = max(dd, peak - eq)
    usd = sum(t[key] * t["risk_usd"] for t in trades)
    return {"n": n, "win_pct": round(len(wins) / n * 100, 1), "avg_R": round(sum(rs) / n, 3),
            "total_R": round(sum(rs), 1), "pf": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_dd_R": round(dd, 1), "usd": round(usd, 0)}


def fmt(st):
    if not st.get("n"):
        return "сделок нет"
    pf = f", PF {st['pf']}" if st.get("pf") is not None else ""
    usd = f"{st['usd']:+,.0f}".replace(",", " ")
    return (f"{st['n']} сделок, прибыльных {st['win_pct']}%, в среднем {st['avg_R']:+} R, "
            f"итого {st['total_R']:+} R ({usd[0]}${usd[1:]}){pf}, макс. просадка {st['max_dd_R']} R")


def report(results, days, errors, start_ms):
    d0 = datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc).strftime("%d.%m.%Y")
    lines = [f"🧪 Бэктест правил за {days} дней (с {d0}), 16 монет, 4ч-свечи, без моделей",
             "R — в рисках на сделку после издержек; $ — при капитале $10 000 и риске 1% (0.5% «отскок»).", ""]
    for name, (trades, rejects) in results.items():
        lines.append(f"▶ {NAMES[name]}: {fmt(stats(trades))}")
    base = results["base"][0]
    if base:
        lines += ["", "Разрез варианта «как сейчас»:"]
        for label, sub in (("по тренду", [t for t in base if not t["bounce"]]),
                           ("«отскок»", [t for t in base if t["bounce"]]),
                           ("лонги", [t for t in base if t["direction"] == "long"]),
                           ("шорты", [t for t in base if t["direction"] == "short"])):
            lines.append(f"  {label}: {fmt(stats(sub))}")
        mid = start_ms + (max(t["opened"] for t in base) - start_ms) // 2
        lines.append(f"  первая половина периода: {fmt(stats([t for t in base if t['opened'] < mid]))}")
        lines.append(f"  вторая половина периода: {fmt(stats([t for t in base if t['opened'] >= mid]))}")
        lines += ["", "Способы выхода («как сейчас», те же сделки):"]
        for key, label in (("R", "держать до дальней цели"), ("R_partial_t1", "половина на цели 1, остаток в безубыток"),
                           ("R_t1_all", "всё на цели 1")):
            lines.append(f"  {label}: {fmt(stats(base, key))}")
        by = {}
        for t in base:
            by.setdefault(t["label"], 0); by[t["label"]] += 1
        lines.append(f"  чем кончались: {', '.join(f'{k} {v}' for k, v in sorted(by.items()))}")
        lines.append(f"  средний срок в сделке: {round(sum(t['held_days'] for t in base) / len(base), 1)} дн.")
        rej = results["base"][1]
        if rej:
            lines.append("  отсеяно гейтами: " + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])))
    if errors:
        lines += ["", "Не загрузились: " + "; ".join(errors)]
    lines += ["", "Ограничения: нет моделей, новостей, вето экспертов и дневного стопа; вход по закрытию свечи; "
              "финансирование оценено средней ставкой. Прошлое не гарантирует будущего."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    print(f"Загрузка данных за {a.days} дней ...")
    data, btc_daily, errors = load_data(a.days)
    if "BTC/USDT" not in data or not btc_daily:
        print("нет данных BTC — бэктест невозможен"); sys.exit(1)
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    start_ms = now_ms - a.days * DAY_MS
    results = {}
    for name, v in VARIANTS.items():
        trades, rejects = run_variant(data, btc_daily, start_ms, v)
        results[name] = (trades, rejects)
        print(f"  {name}: {fmt(stats(trades))}")
    text = report(results, a.days, errors, start_ms)
    print("\n" + text)
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    with open(os.path.join(OUT_DIR, f"report_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(OUT_DIR, f"trades_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({k: v[0] for k, v in results.items()}, f, ensure_ascii=False)
    if not a.no_telegram:
        import notify
        notify.send(text)


if __name__ == "__main__":
    main()
