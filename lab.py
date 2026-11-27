# -*- coding: utf-8 -*-
"""
ЛАБОРАТОРИЯ СТРАТЕГИЙ: ищем правила с преимуществом, честно — без подгонки под прошлое.

Схема проверки (walk-forward):
  год 1 — подбор: у каждого семейства пробуем несколько параметров и выбираем лучший;
  год 2 — проверка: выбранный вариант прогоняем на данных, которых он «не видел».
Засчитываем только то, что в плюсе на ОБОИХ годах и даёт достаточно сделок.

Семейства (все — известные подходы к трендовой торговле, без моделей):
  current_trail — нынешние правила входа, но без цели: прибыль ведёт трейлинг-стоп (3 ATR)
  breakout      — пробой канала Дончиана за N дней (вход по закрытию 4ч выше/ниже канала),
                  стоп 2 ATR, выход по трейлингу k ATR («черепахи»)
  pullback      — откат в тренде: дневной тренд (цена и SMA20 > SMA50), на 4ч цена касается SMA30
                  и закрывается выше; стоп под свингом, цель 2R или трейлинг
  momentum      — дневной импульс: доходность за N дней и цена выше SMA50 — лонг (и зеркально шорт),
                  стоп 2.5 ATR(дн), трейлинг

Общее с живой системой: риск 1% на сделку, heat ≤ 4%, ≤ 3 позиции в одну сторону, одна позиция на монету,
потолок концентрации 15%, издержки 0.09% на сторону + финансирование лонга, стоп приоритетнее цели в свече,
вход по закрытию свечи сигнала. Предел удержания — 60 дней.

Запуск: python lab.py [--days 730] [--no-telegram]
"""

import argparse
import datetime
import json
import math
import os
import sys

import backtest as B

H4 = 4 * 3600 * 1000
DAY = 24 * 3600 * 1000
MAX_HOLD_DAYS = 60
MIN_TRADES_YEAR = 25


# ── индикаторы (заранее, чтобы прогон был быстрым) ───────────────────────────

def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def atr(c, n=14):
    tr = [c[0][2] - c[0][3]] + [max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
                               for i in range(1, len(c))]
    return sma(tr, n)


def to_daily(c4):
    days, cur = [], None
    for c in c4:
        d = c[0] // DAY * DAY
        if cur is None or cur[0] != d:
            if cur:
                days.append(cur)
            cur = [d, c[1], c[2], c[3], c[4], c[5], 1]
        else:
            cur[2] = max(cur[2], c[2]); cur[3] = min(cur[3], c[3]); cur[4] = c[4]; cur[6] += 1
    if cur:
        days.append(cur)
    return [d[:6] for d in days if d[6] == 6]          # только полные дни


def prep(cs):
    closes = [c[4] for c in cs]
    daily = to_daily(cs)
    dc = [d[4] for d in daily]
    p = {"c": cs, "close": closes, "sma10": sma(closes, 10), "sma30": sma(closes, 30), "atr": atr(cs, 14),
         "daily": daily, "d_close": dc, "d_sma20": sma(dc, 20), "d_sma50": sma(dc, 50), "d_atr": atr(daily, 14) if daily else [],
         "d_idx": {}}
    # индекс последнего ЗАКРЫТОГО дня для каждой 4ч-свечи
    j = -1
    for i, c in enumerate(cs):
        close_ts = c[0] + H4
        while j + 1 < len(daily) and daily[j + 1][0] + DAY <= close_ts:
            j += 1
        p["d_idx"][i] = j
    return p


# ── сигналы ─────────────────────────────────────────────────────────────────

def sig_current(p, i, prm, reg):
    """Нынешние правила входа (как backtest.build_setup), но выход трейлингом."""
    cs = p["c"]
    if i < 80:
        return None
    md = B.digest(cs[i - 79:i + 1])
    for d, bounce in B.allowed(md, reg, True):
        st = B.build_setup(md, d, B.VARIANTS["base"])
        if st and "reject" not in st:
            return {"direction": d, "stop": st["stop"], "bounce": bounce, "target": None,
                    "trail": prm["trail"], "atr": p["atr"][i]}
    return None


def sig_breakout(p, i, prm, reg):
    j = p["d_idx"].get(i, -1)
    n = prm["n"]
    if j < n + 1 or p["atr"][i] is None:
        return None
    hi = max(d[2] for d in p["daily"][j - n + 1:j + 1])
    lo = min(d[3] for d in p["daily"][j - n + 1:j + 1])
    c, prev = p["close"][i], p["close"][i - 1]
    a = p["d_atr"][j] or p["atr"][i] * 2.4
    if c > hi and prev <= hi and prm["side"] in ("both", "long"):
        return {"direction": "long", "stop": c - 2 * a, "bounce": False, "target": None, "trail": prm["trail"], "atr": a}
    if c < lo and prev >= lo and prm["side"] in ("both", "short"):
        return {"direction": "short", "stop": c + 2 * a, "bounce": False, "target": None, "trail": prm["trail"], "atr": a}
    return None


def sig_pullback(p, i, prm, reg):
    j = p["d_idx"].get(i, -1)
    if j < 50 or i < 30 or p["sma30"][i] is None or p["d_sma50"][j] is None:
        return None
    dc, d20, d50 = p["d_close"][j], p["d_sma20"][j], p["d_sma50"][j]
    c = p["c"][i]; s30 = p["sma30"][i]; a = p["atr"][i]
    swing_lo = min(x[3] for x in p["c"][i - 19:i + 1]); swing_hi = max(x[2] for x in p["c"][i - 19:i + 1])
    if dc > d50 and d20 > d50 and c[3] <= s30 < c[4] and p["sma10"][i] > s30:
        stop = min(swing_lo, c[4] - 1.0 * a)
        return {"direction": "long", "stop": stop, "bounce": False,
                "target": None if prm["exit"] == "trail" else c[4] + 2 * (c[4] - stop), "trail": 3.0, "atr": a}
    if dc < d50 and d20 < d50 and c[2] >= s30 > c[4] and p["sma10"][i] < s30:
        stop = max(swing_hi, c[4] + 1.0 * a)
        return {"direction": "short", "stop": stop, "bounce": False,
                "target": None if prm["exit"] == "trail" else c[4] - 2 * (stop - c[4]), "trail": 3.0, "atr": a}
    return None


def sig_momentum(p, i, prm, reg):
    """Раз в сутки (свеча, закрывающая день)."""
    if (p["c"][i][0] + H4) % DAY:
        return None
    j = p["d_idx"].get(i, -1)
    n = prm["n"]
    if j < max(n, 50) or p["d_sma50"][j] is None or not p["d_atr"][j]:
        return None
    ret = p["d_close"][j] / p["d_close"][j - n] - 1
    c, s50, a = p["d_close"][j], p["d_sma50"][j], p["d_atr"][j]
    if ret > 0 and c > s50:
        return {"direction": "long", "stop": c - 2.5 * a, "bounce": False, "target": None, "trail": prm["trail"], "atr": a}
    if ret < 0 and c < s50 and prm["short"]:
        return {"direction": "short", "stop": c + 2.5 * a, "bounce": False, "target": None, "trail": prm["trail"], "atr": a}
    return None


FAMILIES = {
    "current_trail": (sig_current, [{"trail": 2.0}, {"trail": 3.0}, {"trail": 4.0}],
                      "нынешний вход + трейлинг вместо цели"),
    "breakout": (sig_breakout, [{"n": n, "trail": t, "side": "both"} for n in (20, 55) for t in (3.0, 5.0)]
                 + [{"n": 20, "trail": 3.0, "side": "long"}],
                 "пробой канала Дончиана"),
    "pullback": (sig_pullback, [{"exit": "target"}, {"exit": "trail"}], "откат в дневном тренде"),
    "momentum": (sig_momentum, [{"n": n, "trail": t, "short": s} for n in (20, 60) for t in (3.0, 5.0) for s in (True, False)],
                 "дневной импульс"),
}


# ── выход ───────────────────────────────────────────────────────────────────

def walk(direction, entry, stop, target, trail, a, path):
    """Стоп обновляется по ЗАКРЫТИЮ свечи и действует со следующей. Стоп приоритетнее цели."""
    best = entry
    for c in path:
        h, l, cl = c[2], c[3], c[4]
        if direction == "long":
            if l <= stop:
                return stop, c[0] + H4, "СТОП"
            if target and h >= target:
                return target, c[0] + H4, "ЦЕЛЬ"
            best = max(best, cl)
            if trail:
                stop = max(stop, best - trail * a)
        else:
            if h >= stop:
                return stop, c[0] + H4, "СТОП"
            if target and l <= target:
                return target, c[0] + H4, "ЦЕЛЬ"
            best = min(best, cl)
            if trail:
                stop = min(stop, best + trail * a)
    return (path[-1][4], path[-1][0] + H4, "СРОК") if path else None


def run(prepared, btc_daily, start_ms, end_ms, fam, prm):
    sig = FAMILIES[fam][0]
    steps = sorted({c[0] + H4 for p in prepared.values() for c in p["c"]
                    if (c[0] + H4) % (12 * 3600 * 1000) == 0 and start_ms <= c[0] + H4 < end_ms})
    idx = {s: {c[0]: i for i, c in enumerate(p["c"])} for s, p in prepared.items()}
    open_pos, trades = [], []
    for t in steps:
        open_pos = [x for x in open_pos if x["exit_ts"] > t]
        reg = B.regime_at(btc_daily, t)
        for s in B.WATCHLIST:
            p = prepared.get(s)
            if not p:
                continue
            i = idx[s].get(t - H4)
            if i is None:
                continue
            g = sig(p, i, prm, reg)
            if not g:
                continue
            d, entry = g["direction"], p["close"][i]
            unit = abs(entry - g["stop"])
            if unit <= 0 or (d == "long") != (g["stop"] < entry):
                continue
            if any(x["symbol"] == s for x in open_pos) or sum(x["direction"] == d for x in open_pos) >= B.SAME_DIR_CAP:
                continue
            if g["bounce"] and sum(x["bounce"] for x in open_pos) >= B.BOUNCE_CAP:
                continue
            risk_t = (B.BOUNCE_RISK_PCT if g["bounce"] else B.RISK_PCT) / 100 * B.CAPITAL
            size = min(risk_t / unit, B.CONC_CAP / entry)
            risk_usd = size * unit
            if sum(x["risk_usd"] for x in open_pos) + risk_usd > B.MAX_HEAT / 100 * B.CAPITAL + 1e-9:
                continue
            path = [c for c in p["c"][i + 1:] if c[0] < t + MAX_HOLD_DAYS * DAY]
            w = walk(d, entry, g["stop"], g.get("target"), g.get("trail"), g["atr"], path)
            if not w:
                continue
            exit_px, exit_ts, label = w
            held = (exit_ts - t) / DAY
            sign = 1 if d == "long" else -1
            r = (sign * (exit_px - entry) - (entry + exit_px) * B.COST_SIDE) / unit
            if d == "long":
                r -= entry * B.FUNDING_8H * 3 * held / unit
            trades.append({"symbol": s, "direction": d, "bounce": g["bounce"], "opened": t, "exit_ts": exit_ts,
                           "label": label, "held_days": round(held, 2), "R": round(r, 3), "risk_usd": round(risk_usd, 2)})
            open_pos.append({"symbol": s, "direction": d, "bounce": g["bounce"], "risk_usd": risk_usd, "exit_ts": exit_ts})
    return trades


def prm_txt(prm):
    m = {"trail": "трейлинг {} ATR", "n": "N={}", "side": "{}", "exit": "выход: {}", "short": "шорты: {}"}
    tr = {"both": "лонг+шорт", "long": "только лонг", "target": "цель 2R", "trail": "трейлинг 3 ATR",
          True: "да", False: "нет"}
    return ", ".join(m[k].format(tr.get(v, v) if k != "trail" or prm.get("exit") != "target" else v)
                     for k, v in prm.items())


def tstat(trades):
    n = len(trades)
    if n < 2:
        return 0.0
    rs = [t["R"] for t in trades]
    m = sum(rs) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
    return round(m / (sd / math.sqrt(n)), 2) if sd else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    data, btc_daily, errors = B.load_data(a.days + 60)             # +60 дней на разгон дневных индикаторов
    prepared = {s: prep(cs) for s, cs in data.items()}
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000) // H4 * H4
    start = now - a.days * DAY
    mid = start + (now - start) // 2
    y1_end = mid                                                   # сделки года 1 открываются до середины
    lines = [f"🔬 Лаборатория стратегий: {a.days} дней, 16 монет. Подбор — на 1-м году, проверка — на 2-м.",
             "В среднем R на сделку после издержек; t — надёжность (|t| ≥ 2 — вряд ли случайность).", ""]
    summary = {}
    for fam, (_sig, grid, title) in FAMILIES.items():
        rows = []
        for prm in grid:
            t1 = run(prepared, btc_daily, start, y1_end, fam, prm)
            t2 = run(prepared, btc_daily, mid, now, fam, prm)
            rows.append((prm, t1, t2))
        ok = [r for r in rows if len(r[1]) >= MIN_TRADES_YEAR] or rows
        prm, t1, t2 = max(ok, key=lambda r: B.stats(r[1]).get("avg_R", -9))
        s1, s2 = B.stats(t1), B.stats(t2)
        passed = s1.get("avg_R", -1) > 0 and s2.get("avg_R", -1) > 0 and len(t2) >= MIN_TRADES_YEAR
        summary[fam] = {"params": prm, "y1": s1, "y2": s2, "t2": tstat(t2), "passed": passed}
        lines.append(f"{'✅' if passed else '❌'} {title} — выбран: {prm_txt(prm)}")
        lines.append(f"   год 1 (подбор): {B.fmt(s1)}")
        lines.append(f"   год 2 (проверка): {B.fmt(s2)}, t={tstat(t2)}")
        if t2:
            lo = [t for t in t2 if t["direction"] == "long"]; sh = [t for t in t2 if t["direction"] == "short"]
            lines.append(f"   год 2 по сторонам: лонги {B.stats(lo).get('avg_R', '—')} R ({len(lo)}), "
                         f"шорты {B.stats(sh).get('avg_R', '—')} R ({len(sh)}); средний срок "
                         f"{round(sum(t['held_days'] for t in t2) / len(t2), 1)} дн.")
        others = [f"{prm_txt(p)}: {B.stats(x).get('avg_R', '—')}/{B.stats(y).get('avg_R', '—')}" for p, x, y in rows if p is not prm]
        if others:
            lines.append("   другие параметры (год 1 / год 2, R на сделку): " + "; ".join(others))
        lines.append("")
    bh = [d for d in btc_daily if d[0] >= start]
    if bh:
        lines.append(f"Для сравнения: BTC за период {round((bh[-1][4] / bh[0][4] - 1) * 100, 1)}% "
                     f"(год 1: {round((min(bh, key=lambda d: abs(d[0] - mid))[4] / bh[0][4] - 1) * 100, 1)}%).")
    if errors:
        lines.append("Не загрузились: " + "; ".join(errors))
    lines.append("Ограничения: без моделей и новостей, финансирование — средняя ставка, вход по закрытию свечи. "
                 "Прошлое не гарантирует будущего.")
    text = "\n".join(lines)
    print(text)
    os.makedirs(B.OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    with open(os.path.join(B.OUT_DIR, f"lab_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(B.OUT_DIR, f"lab_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, default=str)
    if not a.no_telegram:
        import notify
        notify.send(text)


if __name__ == "__main__":
    main()
