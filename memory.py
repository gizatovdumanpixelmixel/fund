# -*- coding: utf-8 -*-
"""
ПАМЯТЬ СИСТЕМЫ: как агенты учатся на прошлом.

Модели сами между запусками ничего не помнят. «Обучение» здесь — это данные,
которые код собирает и подаёт агентам в следующий раз:

  decisions.jsonl         — журнал решений: что предлагал каждый агент по каждой монете
  lessons.jsonl           — уроки по закрытым сделкам: что было, почему, правило на будущее
  rejected_reviews.jsonl  — разбор НЕОТКРЫТЫХ сетапов: отклонённых (прав ли гейт) и
                            одобренных, но не открытых (сработала бы идея)
                            (так данные копятся быстрее, чем по одним сделкам)

  lessons_for(монета)  — 3 урока по этой монете + 2 по другим, свежие весят больше
  scorecard()          — табель: как часто агент оказывался прав

Уроки строит КОД по фактам (режим рынка, тренд, ширина стопа, итог), а не модель:
так урок нельзя «выдумать».

Команды:
  python3 memory.py review     — разобрать новые закрытые сделки и отклонённые сетапы
  python3 memory.py lessons    — показать уроки
  python3 memory.py scorecard  — показать табель агентов
"""

import os, sys, json, datetime

import positions as P
import context as C

BASE = os.path.dirname(os.path.abspath(__file__))
DECISIONS_PATH = os.path.join(BASE, "decisions.jsonl")
LESSONS_PATH = os.path.join(BASE, "lessons.jsonl")
REJECTED_PATH = os.path.join(BASE, "rejected_reviews.jsonl")

MIN_STOP_VS_CANDLE = 0.5      # как в мандате pipeline.py
REJECTED_MIN_AGE_H = 24       # отклонённый сетап разбираем не раньше, чем через сутки
REJECTED_MAX_DAYS = 14        # если за 2 недели ни стоп, ни цель — «не решилось»
MIN_DECISIONS_FOR_SCORE = 10
HALF_LIFE_DAYS = 30           # урок месячной давности весит вдвое меньше свежего


# ── файлы ────────────────────────────────────────────────────────────────────

def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _dt(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None


# ── журнал решений ───────────────────────────────────────────────────────────

def log_decision(rec):
    """Пишет решение по монете за один запуск. id = <run_id>:<монета>."""
    rec = dict(rec)
    rec.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
    rec["id"] = f"{rec['run_id']}:{rec['instrument']}"
    append(DECISIONS_PATH, rec)
    return rec["id"]


# ── уроки по закрытым сделкам ────────────────────────────────────────────────

def _avg_range_before(exchange, symbol, when):
    since = int((when - datetime.timedelta(hours=4 * 20)).timestamp() * 1000)
    o = [c for c in exchange.fetch_ohlcv(symbol, timeframe="4h", since=since, limit=30)
         if c[0] < when.timestamp() * 1000][-14:]
    return sum(c[2] - c[3] for c in o) / len(o) if o else None


def build_lesson(trade, regime, avg_range):
    """Урок по фактам сделки. regime — режим BTC на момент входа, avg_range —
    средний размах 4ч свечи перед входом. Возвращает dict урока."""
    d = trade["direction"]
    won = (trade.get("actual_R") or 0) > 0
    stop_dist = abs(trade["entry"] - trade["stop"])
    tags = []
    against = (regime == "up" and d == "short") or (regime == "down" and d == "long")
    if against:
        tags.append("против режима рынка")
    tight = bool(avg_range) and stop_dist < MIN_STOP_VS_CANDLE * avg_range
    if tight:
        tags.append("узкий стоп")

    side = C.DIR_RU.get(d, d).capitalize()
    reg_ru = C.REGIME_RU.get(regime, "неизвестный")
    reg_prep = {"up": "восходящем", "down": "нисходящем", "range": "боковом"}.get(regime, "неизвестном")
    forecast = trade.get("forecast_R_net")
    what = (f"{side} {trade['instrument']} при {reg_prep} режиме BTC: {trade.get('hit', '')} "
            f"({trade.get('actual_R')} R" + (f", прогноз был {forecast} R" if forecast is not None else "") + ").")
    if won:
        rule = (f"Когда режим BTC {reg_ru} — {C.DIR_RU.get(d, d)} по тренду рабочий вариант; "
                f"такие сетапы держать в приоритете.")
    elif against:
        rule = f"Когда режим BTC {reg_ru} — не открывать {C.DIR_RU.get(d, d)}."
    elif tight:
        rule = ("Когда стоп ближе половины среднего размаха свечи — пропускать сетап: "
                "его выбивает обычный шум.")
    else:
        rule = ("Явной ошибки нет: стоп сработал как задумано. Одна такая сделка — "
                "не повод менять правила.")
    return {"trade_id": trade.get("id"), "decision_id": trade.get("decision_id"),
            "instrument": trade["instrument"], "direction": d, "outcome": "win" if won else "loss",
            "actual_R": trade.get("actual_R"), "regime_at_entry": regime, "tags": tags,
            "what_happened": what, "rule": rule,
            "closed_at": trade.get("closed_at"), "created": datetime.datetime.now().isoformat(timespec="seconds")}


def review_closed(exchange=None):
    """Разбирает закрытые сделки, по которым ещё нет урока. Возвращает новые уроки."""
    exchange = exchange or P.exchange()
    done = {l.get("trade_id") for l in load(LESSONS_PATH)}
    new = []
    for t in P.load_results():
        if t.get("id") in done or not all(k in t for k in ("entry", "stop", "direction", "instrument")):
            continue
        opened = _dt(t.get("opened_at"))
        regime = avg = None
        if opened:
            try:
                regime = C.regime_at(exchange, opened)
                avg = _avg_range_before(exchange, t["instrument"], opened)
            except Exception:                        # noqa: BLE001
                pass
        lesson = build_lesson(t, regime, avg)
        append(LESSONS_PATH, lesson)
        new.append(lesson)
    return new


def lessons_for(instrument, k_same=3, k_other=2, now=None):
    """Уроки для промпта: по этой монете и по другим, свежие весят больше."""
    now = now or datetime.datetime.now()

    def weight(l):
        d = _dt(l.get("closed_at")) or _dt(l.get("created")) or now
        return 0.5 ** (max((now - d).days, 0) / HALF_LIFE_DAYS)

    ls = sorted(load(LESSONS_PATH), key=weight, reverse=True)
    pick = [l for l in ls if l["instrument"] == instrument][:k_same] + \
           [l for l in ls if l["instrument"] != instrument][:k_other]
    return [{"instrument": l["instrument"], "what_happened": l["what_happened"], "rule": l["rule"],
             "weight": round(weight(l), 2)} for l in pick]


# ── отклонённые сетапы ───────────────────────────────────────────────────────

def review_rejected(exchange=None, now=None):
    """Проверяет по свечам, что стало бы с отклонёнными сетапами. Возвращает новые разборы."""
    exchange = exchange or P.exchange()
    now = now or datetime.datetime.now()
    done = {r["decision_id"] for r in load(REJECTED_PATH)}
    # реально открытые сделки разбирает review_closed — здесь их пропускаем
    opened = {t.get("decision_id") for t in P.load_results()}
    try:
        opened |= {x.get("decision_id") for x in P.load_portfolio().get("open_positions", [])}
    except Exception:                                # noqa: BLE001
        pass
    new = []
    for d in load(DECISIONS_PATH):
        if d["id"] in done or d["id"] in opened or not d.get("setup_found"):
            continue
        # одобренная, но не открытая идея: проверяем «в тени», чтобы учиться и на ней
        approved = bool(d.get("passed")) and d.get("expert_allowed") is True \
            and d.get("consultant_verdict") != "veto"
        if d.get("entry") is None or d.get("stop") is None or d.get("target_far") is None \
                or d.get("direction") not in ("long", "short"):
            continue
        ts = _dt(d["ts"])
        if not ts or (now - ts).total_seconds() < REJECTED_MIN_AGE_H * 3600:
            continue
        pseudo = {"direction": d["direction"], "stop": d["stop"], "target_far": d["target_far"],
                  "entry": d["entry"], "targets": [d["target_far"]]}
        first_full = (int(ts.timestamp() * 1000) // P.HOUR_MS + 1) * P.HOUR_MS
        try:
            candles = P.fetch_hourly_since(d["instrument"], first_full)
        except Exception:                            # noqa: BLE001
            continue
        outcome = None
        for _ts, _o, high, low, _c, _v in candles:
            hit = P.range_hit(pseudo, high, low)
            if hit:
                outcome = "would_stop" if hit[0] == "СТОП" else "would_target"
                break
        if outcome is None:
            if (now - ts).days < REJECTED_MAX_DAYS:
                continue                              # ещё рано судить
            outcome = "unresolved"
        verdicts = ({"would_stop": "одобренная идея ушла бы в стоп",
                     "would_target": "одобренная идея дошла бы до цели",
                     "unresolved": "ни стоп, ни цель за 2 недели"} if approved else
                    {"would_stop": "гейт был прав: сделка ушла бы в стоп",
                     "would_target": "гейт отсёк прибыльную сделку",
                     "unresolved": "ни стоп, ни цель за 2 недели"})
        rec = {"decision_id": d["id"], "instrument": d["instrument"], "direction": d["direction"],
               "kind": "approved" if approved else "rejected",
               "reasons": d.get("reasons", []), "outcome": outcome,
               "verdict": verdicts[outcome],
               "reviewed": now.isoformat(timespec="seconds")}
        append(REJECTED_PATH, rec)
        new.append(rec)
    return new


# ── табель агентов ───────────────────────────────────────────────────────────

def _outcomes():
    """decision_id → 'target' | 'stop' по реальным сделкам и разбору отклонённых."""
    out = {}
    for t in P.load_results():
        if t.get("decision_id"):
            out[t["decision_id"]] = "target" if (t.get("actual_R") or 0) > 0 else "stop"
    for r in load(REJECTED_PATH):
        if r["outcome"] in ("would_stop", "would_target"):
            out[r["decision_id"]] = "target" if r["outcome"] == "would_target" else "stop"
    return out


def scorecard():
    """Как часто каждый участник оказывался прав. Пока решений мало — помечаем."""
    outcomes = _outcomes()
    rows = {"Технический аналитик (сетап сработал)": [0, 0],
            "Гейты кода (отклонили не зря)": [0, 0],
            "Консультант №2 (pass/veto)": [0, 0],
            "Эксперт №1 (можно/нельзя)": [0, 0]}
    for d in load(DECISIONS_PATH):
        res = outcomes.get(d["id"])
        if not res or not d.get("setup_found"):
            continue
        good = res == "target"
        rows["Технический аналитик (сетап сработал)"][0] += 1
        rows["Технический аналитик (сетап сработал)"][1] += good
        if not d.get("passed"):
            rows["Гейты кода (отклонили не зря)"][0] += 1
            rows["Гейты кода (отклонили не зря)"][1] += not good
        v = d.get("consultant_verdict")   # «unchecked» (лимит модели) в табель не идёт
        if v in ("pass", "veto"):
            rows["Консультант №2 (pass/veto)"][0] += 1
            rows["Консультант №2 (pass/veto)"][1] += (v == "pass") == good
        a = d.get("expert_allowed")
        if isinstance(a, bool):
            rows["Эксперт №1 (можно/нельзя)"][0] += 1
            rows["Эксперт №1 (можно/нельзя)"][1] += a == good
    table = []
    for name, (n, ok) in rows.items():
        table.append({"agent": name, "n": n, "correct": ok,
                      "accuracy_pct": round(ok / n * 100, 1) if n else None,
                      "note": "мало данных" if n < MIN_DECISIONS_FOR_SCORE else ""})
    return table


# ── терминал ─────────────────────────────────────────────────────────────────

def _print_lessons():
    ls = load(LESSONS_PATH)
    if not ls:
        print("Уроков пока нет."); return
    for l in ls:
        print(f"• {l['what_happened']}\n  → {l['rule']}" + (f"  [{', '.join(l['tags'])}]" if l["tags"] else ""))


def _print_scorecard():
    for r in scorecard():
        acc = f"{r['accuracy_pct']}%" if r["accuracy_pct"] is not None else "—"
        print(f"  {r['agent']:42} {r['correct']}/{r['n']}  {acc}  {r['note']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "review":
        nl = review_closed(); nr = review_rejected()
        print(f"Новых уроков: {len(nl)}, разобрано отклонённых сетапов: {len(nr)}\n")
        _print_lessons()
    elif cmd == "lessons":
        _print_lessons()
    elif cmd == "scorecard":
        _print_scorecard()
    else:
        print("Команды: python3 memory.py [review | lessons | scorecard]")
