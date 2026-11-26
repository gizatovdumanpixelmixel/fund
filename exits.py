"""Какой способ выхода из сделки зарабатывает больше — проверка на реальных сделках.

Для каждой закрытой сделки код проигрывает часовые свечи с момента входа и считает в R
(после издержек), чем закончилась бы сделка при трёх способах выхода:
  plan        — вся позиция держится до дальней цели или стопа (как сейчас считает система);
  partial_t1  — половина фиксируется на первой цели, стоп остатка переносится на вход;
  t1_all      — вся позиция закрывается на первой цели.
Плюс фактический результат (actual_R), включая ручные закрытия.

Правило осторожности как в positions.py: если в одной свече задеты и стоп, и цель — считаем стоп.
Горизонт свинга — 15 дней: не решилось за это время — оценка по последней цене.
Через 20+ сделок видно, какой способ выхода выгоднее именно для этой системы.
"""
from __future__ import annotations

import datetime
import glob
import json
import os

import positions as P

EXITS_PATH = "exits.jsonl"
HORIZON_DAYS = 15
STRATEGIES = ("plan", "partial_t1", "t1_all")
NAMES = {"plan": "держать до дальней цели", "partial_t1": "половина на цели 1, остаток в безубыток",
         "t1_all": "всё на цели 1", "actual": "как закрыл ты"}


def _r(direction, entry, stop, exit_price, cost_pct=P.COST_PCT_PER_SIDE):
    """Результат в R на единицу позиции после издержек на вход и выход."""
    sign = 1 if direction == "long" else -1
    unit = abs(entry - stop)
    gross = sign * (exit_price - entry)
    costs = (entry + exit_price) * cost_pct / 100.0
    return (gross - costs) / unit


def simulate(direction, entry, stop, targets, candles):
    """candles: [[ts, o, h, l, c, v], ...] с момента входа. Возвращает {стратегия: R} и resolved."""
    if not targets or not candles:
        return None
    sign = 1 if direction == "long" else -1
    tg = sorted(targets, key=lambda t: sign * t)
    t1, tfar = tg[0], tg[-1]

    def touched(level, high, low, up):          # up=True — уровень выше цены (цель лонга / стоп шорта)
        return high >= level if up else low <= level

    out, resolved = {}, {}
    last = candles[-1][4]
    # plan и t1_all: один выход
    for name, tgt in (("plan", tfar), ("t1_all", t1)):
        res = None
        for _ts, _o, h, l, _c, _v in candles:
            if touched(stop, h, l, up=(direction == "short")):
                res = _r(direction, entry, stop, stop); break
            if touched(tgt, h, l, up=(direction == "long")):
                res = _r(direction, entry, stop, tgt); break
        resolved[name] = res is not None
        out[name] = round(res if res is not None else _r(direction, entry, stop, last), 2)
    # partial_t1: половина на t1, остаток со стопом на входе до дальней цели
    half_done, res, cur_stop = False, None, stop
    for _ts, _o, h, l, _c, _v in candles:
        if touched(cur_stop, h, l, up=(direction == "short")):
            rest = _r(direction, entry, stop, cur_stop)
            res = (0.5 * _r(direction, entry, stop, t1) + 0.5 * rest) if half_done else rest
            break
        if not half_done and touched(t1, h, l, up=(direction == "long")):
            half_done, cur_stop = True, entry      # следующая свеча — уже со стопом на входе
            if t1 == tfar:
                res = _r(direction, entry, stop, t1); break
            continue
        if half_done and touched(tfar, h, l, up=(direction == "long")):
            res = 0.5 * _r(direction, entry, stop, t1) + 0.5 * _r(direction, entry, stop, tfar); break
    resolved["partial_t1"] = res is not None
    if res is None:
        mtm = _r(direction, entry, stop, last)
        res = (0.5 * _r(direction, entry, stop, t1) + 0.5 * mtm) if half_done else mtm
    out["partial_t1"] = round(res, 2)
    return {"R": out, "resolved": resolved, "t1": t1, "t_far": tfar}


def _targets_for(rec):
    """Цели сделки: из самой записи, иначе из отчёта, по которому её открыли."""
    if rec.get("targets"):
        return rec["targets"]
    src = rec.get("source") or ""
    if ":" not in src:
        return None
    stamp, cid = src.split(":", 1)
    path = os.path.join(os.path.dirname(os.path.abspath(P.__file__)), "briefings", f"briefing_{stamp}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        pkg = json.load(f).get("package") or {}
    for c in pkg.get("candidates") or []:
        if c.get("id") == cid:
            vals = c.get("targets") or []
            return [float(t["value"] if isinstance(t, dict) else t) for t in vals if t is not None] or None
    return None


def load():
    if not os.path.exists(EXITS_PATH):
        return []
    with open(EXITS_PATH, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def backfill(fetch=None, now=None):
    """Считает способы выхода для закрытых сделок, по которым ещё нет итогов (или итоги не решились)."""
    fetch = fetch or P.fetch_hourly_since
    now = now or datetime.datetime.now()
    done = {r["id"]: r for r in load()}
    changed = False
    for rec in P.load_results():
        old = done.get(rec["id"])
        if old and all(old["resolved"].values()):
            continue
        targets = _targets_for(rec)
        if not targets:
            continue
        opened = datetime.datetime.fromisoformat(rec["opened_at"])
        start = int(opened.timestamp() * 1000) // P.HOUR_MS * P.HOUR_MS + P.HOUR_MS
        end = opened + datetime.timedelta(days=HORIZON_DAYS)
        try:
            candles = [c for c in fetch(rec["instrument"], start) if c[0] <= end.timestamp() * 1000]
        except Exception as e:                       # noqa: BLE001
            print(f"  выходы {rec['id']}: нет свечей ({e})")
            continue
        sim = simulate(rec["direction"], rec["entry"], rec["stop"], targets, candles)
        if not sim:
            continue
        done[rec["id"]] = {"id": rec["id"], "instrument": rec["instrument"], "direction": rec["direction"],
                           "hit": rec.get("hit"), "actual": rec["actual_R"], **sim,
                           "final": now >= end, "computed": now.isoformat(timespec="minutes")}
        changed = True
    if changed:
        with open(EXITS_PATH, "w", encoding="utf-8") as f:
            for r in done.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return list(done.values())


def summary_text(rows=None, min_n=20):
    rows = rows if rows is not None else load()
    if not rows:
        return ""
    n = len(rows)
    tot = {s: round(sum(r["R"][s] for r in rows), 2) for s in STRATEGIES}
    tot["actual"] = round(sum(r["actual"] for r in rows), 2)
    best = max(tot, key=tot.get)
    lines = [f"\n🎯 Как лучше выходить из сделок ({n} сделок, сумма R после издержек)"]
    for k in ("actual", "plan", "partial_t1", "t1_all"):
        mark = " ← лучше всех" if k == best else ""
        lines.append(f"  {NAMES[k]}: {tot[k]:+} R (в среднем {tot[k] / n:+.2f} R){mark}")
    manual = [r for r in rows if r.get("hit") == "ВРУЧНУЮ"]
    if manual:
        diff = round(sum(r["R"]["plan"] - r["actual"] for r in manual), 2)
        lines.append(f"  Ручные закрытия ({len(manual)}): держа по плану, получил бы на {diff:+} R "
                     f"{'больше' if diff >= 0 else 'меньше'}")
    open_n = sum(1 for r in rows if not all(r["resolved"].values()))
    if open_n:
        lines.append(f"  {open_n} сделок ещё не решились за 15 дней — оценены по последней цене")
    if n < min_n:
        lines.append(f"  Пока мало сделок для вывода (нужно от {min_n}) — правило не меняем")
    return "\n".join(lines)
