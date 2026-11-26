# -*- coding: utf-8 -*-
"""
СОПРОВОЖДЕНИЕ ПОЗИЦИЙ + ПЕТЛЯ РЕЗУЛЬТАТА (фаза 1a, бумага).

Основной pipeline.py даёт совет. Этот файл ведёт сделку дальше:
  - open   : предвходовая проверка по ТЕКУЩЕЙ цене и запись позиции
  - check  : проверка по ИСТОРИИ часовых свечей с момента открытия:
             стоп или цель, задетые пока ноутбук был выключен, не теряются
  - stats  : сводка по закрытым сделкам (винрейт, матожидание)

Денег не тратит: это бумажный учёт. Реальные сделки исполняете вы сами.
Панель (app.py) использует эти же функции, поэтому правила везде одинаковые.

Запуск:
  python3 positions.py open
  python3 positions.py check
  python3 positions.py stats

Правила сопровождения:
  - позиция держится до СТОПА или до ДАЛЬНЕЙ цели (по ней же считается прогноз R);
  - если в одной часовой свече задеты и стоп, и цель — засчитывается стоп;
  - свеча, внутри которой открылись, не проверяется: часть её движения была до входа.
"""

import os, sys, json, glob, datetime

EXCHANGE_ID = "binance"        # держите таким же, как в pipeline.py
CAPITAL_DEFAULT = 10000
MAX_HEAT_PCT = 4.0
MIN_R = 1.5                    # как min_R в мандате pipeline.py
BOUNCE_CAP = 2                 # как context.BOUNCE_CAP: позиций в режиме «отскок» одновременно
COST_PCT_PER_SIDE = 0.09       # комиссия 0.04 + спред 0.02 + проскальзывание 0.03, как в pipeline.py
MIN_TRADES_FOR_STATS = 20

BASE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(BASE, "portfolio.json")
RESULTS_PATH = os.path.join(BASE, "results.jsonl")
BRIEFINGS_GLOB = os.path.join(BASE, "briefings", "briefing_*.json")
HOUR_MS = 3600 * 1000

_ex = None


def exchange():
    global _ex
    if _ex is None:
        import market
        _ex = market.spot()              # в облаке — зеркало данных Binance (без блокировки)
    return _ex


# ── хранилище ───────────────────────────────────────────────────────────────

def load_portfolio():
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH, encoding="utf-8") as f:
            p = json.load(f)
        p.setdefault("capital", CAPITAL_DEFAULT)
        p.setdefault("open_positions", [])
        return p
    return {"capital": CAPITAL_DEFAULT, "open_positions": [], "current_heat_pct": 0.0}


def save_portfolio(p):
    p["current_heat_pct"] = round(sum(x.get("risk_pct", 0) for x in p["open_positions"]), 3)
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def load_results():
    if not os.path.exists(RESULTS_PATH):
        return []
    with open(RESULTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def log_result(rec):
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def latest_briefing():
    files = sorted(glob.glob(BRIEFINGS_GLOB))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


# ── рынок ────────────────────────────────────────────────────────────────────

def price_now(symbol):
    return float(exchange().fetch_ticker(symbol)["last"])


def fetch_hourly_since(symbol, since_ms):
    """Часовые свечи с указанного момента до сейчас (с дозагрузкой страницами)."""
    out, cursor = [], since_ms
    while True:
        batch = exchange().fetch_ohlcv(symbol, timeframe="1h", since=cursor, limit=1000)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 1
    return out


# ── уровни сделки ────────────────────────────────────────────────────────────

def far_target(direction, targets):
    """Дальняя цель в сторону сделки — по ней считается прогноз R и закрывается позиция."""
    if not targets:
        return None
    return max(targets) if direction == "long" else min(targets)


def r_net(direction, entry, stop, target, funding_frac=0.0):
    """Чистый R при входе по цене entry: прибыль до цели минус издержки
    входа и выхода и плата за финансирование, делённая на риск до стопа."""
    risk_unit = abs(entry - stop)
    if not risk_unit or target is None:
        return 0.0
    reward = (target - entry) if direction == "long" else (entry - target)
    costs = 2 * entry * COST_PCT_PER_SIDE / 100.0 + entry * (funding_frac or 0.0)
    return round((reward - costs) / risk_unit, 2)


def actual_R(pos, exit_price):
    risk_unit = abs(pos["entry"] - pos["stop"])
    reward = (exit_price - pos["entry"]) if pos["direction"] == "long" else (pos["entry"] - exit_price)
    return round(reward / risk_unit, 2) if risk_unit else 0.0


def range_hit(pos, high, low):
    """Задет ли стоп или дальняя цель в диапазоне цен [low, high].
    Если задеты оба — засчитываем СТОП (правило осторожности).
    Возвращает (метка, цена_выхода) или None."""
    stop = pos["stop"]
    target = pos.get("target_far") or far_target(pos["direction"], pos.get("targets", []))
    if pos["direction"] == "long":
        if low <= stop:
            return "СТОП", stop
        if target is not None and high >= target:
            return "ЦЕЛЬ", target
    else:
        if high >= stop:
            return "СТОП", stop
        if target is not None and low <= target:
            return "ЦЕЛЬ", target
    return None


# ── предвходовая проверка ────────────────────────────────────────────────────

def preflight(cand, portfolio, price):
    """Проверка перед записью позиции по ТЕКУЩЕЙ цене (а не по идеальной из отчёта).
    Возвращает dict: ok, reasons, fill, R_net, risk_amount, risk_pct, notes."""
    d, stop = cand["direction"], cand["stop"]
    target = far_target(d, cand.get("targets", []))
    size = cand["size"]["value"]
    cap = portfolio.get("capital", CAPITAL_DEFAULT)
    reasons, notes = [], []

    held = {p["instrument"] for p in portfolio["open_positions"]}
    if cand["instrument"] in held:
        reasons.append(f"по {cand['instrument']} уже есть открытая позиция")

    if cand.get("mode") == "bounce":
        nb = sum(1 for p in portfolio["open_positions"] if p.get("mode") == "bounce")
        if nb >= BOUNCE_CAP:
            reasons.append(f"уже {nb} позиции в режиме «отскок» — лимит {BOUNCE_CAP}")

    if (d == "long" and price <= stop) or (d == "short" and price >= stop):
        reasons.append(f"цена {price} уже за стопом {stop} — сетап сломан")
    if target is not None and ((d == "long" and price >= target) or (d == "short" and price <= target)):
        reasons.append(f"цена {price} уже дошла до цели {target} — движение упущено")

    risk_unit = abs(price - stop)
    risk_amount = round(size * risk_unit, 2)
    risk_pct = round(risk_amount / cap * 100, 3)
    rn = r_net(d, price, stop, target, cand.get("funding_hold_frac", 0.0))
    planned = cand.get("expected_R_net")

    if not reasons:
        if rn < MIN_R:
            reasons.append(f"по текущей цене чистая R = {rn} (в отчёте было {planned}), "
                           f"это ниже порога {MIN_R} — вход уже невыгоден")
        heat = sum(p.get("risk_pct", 0) for p in portfolio["open_positions"])
        if heat + risk_pct > MAX_HEAT_PCT + 1e-9:
            reasons.append(f"занятый риск стал бы {round(heat + risk_pct, 2)}% при лимите {MAX_HEAT_PCT}%")
        drift = abs(price - cand["entry"])
        if drift > 0:
            notes.append(f"вход по {price} вместо {cand['entry']} из отчёта; "
                         f"R {planned} → {rn}, риск ${cand.get('risk_amount')} → ${risk_amount}")

    return {"ok": not reasons, "reasons": reasons, "notes": notes, "fill": price,
            "R_net": rn, "risk_amount": risk_amount, "risk_pct": risk_pct, "target_far": target}


def record_position(cand, sprint_id, check):
    """Записывает позицию по цене предвходовой проверки. Возвращает её ID."""
    p = load_portfolio()
    pid = f"P-{len(p['open_positions']) + len(load_results()) + 1:04d}"
    p["open_positions"].append({
        "id": pid, "opened_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "instrument": cand["instrument"], "direction": cand["direction"],
        "entry": check["fill"], "entry_planned": cand["entry"],
        "stop": cand["stop"], "targets": cand.get("targets", []), "target_far": check["target_far"],
        "size": cand["size"], "risk_amount": check["risk_amount"], "risk_pct": check["risk_pct"],
        "expected_R_net": check["R_net"], "expected_R_net_plan": cand.get("expected_R_net"),
        "status": "open", "source": f"{sprint_id}:{cand['id']}", "decision_id": cand.get("decision_id"),
        "mode": cand.get("mode", "trend"),
    })
    save_portfolio(p)
    return pid


# ── сопровождение ────────────────────────────────────────────────────────────

def check_position(pos):
    """Проверка по истории часовых свечей с момента открытия + по текущей цене.
    Возвращает dict: closed, label, exit, closed_at, price."""
    opened_ms = int(datetime.datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
    first_full = (opened_ms // HOUR_MS + 1) * HOUR_MS
    for ts, _o, high, low, _c, _v in fetch_hourly_since(pos["instrument"], first_full):
        hit = range_hit(pos, high, low)
        if hit:
            when = datetime.datetime.fromtimestamp(ts / 1000).isoformat(timespec="minutes")
            return {"closed": True, "label": hit[0], "exit": hit[1], "closed_at": when, "price": None}
    price = price_now(pos["instrument"])
    hit = range_hit(pos, price, price)
    if hit:
        return {"closed": True, "label": hit[0], "exit": hit[1],
                "closed_at": datetime.datetime.now().isoformat(timespec="minutes"), "price": price}
    return {"closed": False, "label": "открыта", "exit": None, "closed_at": None, "price": price}


def check_all():
    """Проверяет все открытые позиции, закрытые пишет в results.jsonl.
    Возвращает список (вид, текст): вид = info / success / warning / error."""
    p = load_portfolio()
    still_open, messages = [], []
    for pos in p["open_positions"]:
        side = "лонг" if pos["direction"] == "long" else "шорт"
        name = f"{pos['id']} {pos['instrument']} {side}"
        try:
            res = check_position(pos)
        except Exception as e:                       # noqa: BLE001
            messages.append(("error", f"{name}: не удалось получить данные рынка ({e})"))
            still_open.append(pos)
            continue
        if not res["closed"]:
            messages.append(("info", f"{name}: цена {res['price']} — ещё открыта "
                                     f"(вход {pos['entry']}, стоп {pos['stop']}, "
                                     f"цель {pos.get('target_far') or pos.get('targets')})"))
            still_open.append(pos)
            continue
        r = actual_R(pos, res["exit"])
        pnl = round(r * (pos.get("risk_amount") or 0), 2)
        outcome = "прибыль" if r > 0 else "убыток"
        log_result({"id": pos["id"], "instrument": pos["instrument"], "direction": pos["direction"],
                    "entry": pos["entry"], "stop": pos["stop"], "exit": res["exit"], "hit": res["label"],
                    "opened_at": pos["opened_at"], "closed_at": res["closed_at"],
                    "forecast_R_net": pos.get("expected_R_net"), "actual_R": r,
                    "pnl": pnl, "outcome": outcome, "source": pos.get("source"),
                    "decision_id": pos.get("decision_id"), "mode": pos.get("mode", "trend"),
                    "size": (pos.get("size") or {}).get("value")})
        messages.append(("success" if r > 0 else "warning",
                         f"{name}: {res['label']} по {res['exit']} ({res['closed_at'].replace('T', ' ')}) → "
                         f"{outcome} ${pnl} (прогноз R {pos.get('expected_R_net')}, факт R {r})"))
    p["open_positions"] = still_open
    save_portfolio(p)
    return messages


def close_manual(pid):
    """Закрыть позицию вручную по текущей цене (например, по совету монитора).
    Возвращает текст результата."""
    p = load_portfolio()
    pos = next((x for x in p["open_positions"] if x["id"].lower() == pid.lower()), None)
    if not pos:
        return f"Позиции {pid} среди открытых нет."
    price = price_now(pos["instrument"])
    r = actual_R(pos, price)
    pnl = round(r * (pos.get("risk_amount") or 0), 2)
    outcome = "прибыль" if r > 0 else "убыток"
    log_result({"id": pos["id"], "instrument": pos["instrument"], "direction": pos["direction"],
                "entry": pos["entry"], "stop": pos["stop"], "exit": price, "hit": "ВРУЧНУЮ",
                "opened_at": pos["opened_at"],
                "closed_at": datetime.datetime.now().isoformat(timespec="minutes"),
                "forecast_R_net": pos.get("expected_R_net"), "actual_R": r, "pnl": pnl,
                "outcome": outcome, "source": pos.get("source"), "decision_id": pos.get("decision_id"),
                "mode": pos.get("mode", "trend"), "size": (pos.get("size") or {}).get("value")})
    p["open_positions"] = [x for x in p["open_positions"] if x["id"] != pos["id"]]
    save_portfolio(p)
    return f"{pos['id']} {pos['instrument']} закрыта вручную по {price}: {outcome} ${pnl} ({r} R)."


def compute_stats(rows):
    n = len(rows)
    wins = [r for r in rows if r["actual_R"] > 0]
    losses = [r for r in rows if r["actual_R"] <= 0]
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_win": round(sum(r["actual_R"] for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(r["actual_R"] for r in losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(r["actual_R"] for r in rows) / n, 2) if n else 0.0,
        "total_pnl": round(sum(r.get("pnl", 0) for r in rows), 2),
    }


# ── команды для терминала ────────────────────────────────────────────────────

def cmd_open():
    data = latest_briefing()
    if not data or not data.get("package", {}).get("candidates"):
        print("В последнем отчёте нет кандидатов для открытия.")
        return
    cands = data["package"]["candidates"]
    sprint_id = data["package"].get("sprint_id", "")
    print("\nКандидаты из последнего отчёта:\n")
    for i, c in enumerate(cands, 1):
        side = "лонг" if c["direction"] == "long" else "шорт"
        print(f"  [{i}] {c['id']}  {c['instrument']}  {side}  вход~{c['entry']} "
              f"стоп {c['stop']} цели {c.get('targets')}  размер {c['size']['value']} {c['size']['unit_type']}")
    try:
        pick = int(input("\nНомер сделки, в которую вы входите (0 — отмена): "))
    except (ValueError, EOFError):
        print("Отменено."); return
    if pick < 1 or pick > len(cands):
        print("Отменено."); return

    c = cands[pick - 1]
    try:
        price = price_now(c["instrument"])
    except Exception as e:                           # noqa: BLE001
        print(f"Не удалось получить текущую цену: {e}"); return
    check = preflight(c, load_portfolio(), price)
    print(f"\nПредвходовая проверка по текущей цене {price}:")
    for n in check["notes"]:
        print(f"  ⓘ {n}")
    if not check["ok"]:
        for r in check["reasons"]:
            print(f"  ✗ {r}")
        print("\nПозиция НЕ записана.")
        return
    print(f"  ✓ всё в норме: чистая R {check['R_net']}, риск ${check['risk_amount']} ({check['risk_pct']}%)")
    try:
        yes = input("Записать позицию? (д/н): ").strip().lower() in ("д", "да", "y", "yes")
    except EOFError:
        yes = False
    if not yes:
        print("Отменено."); return
    pid = record_position(c, sprint_id, check)
    print(f"\n✓ Записана позиция {pid} по {c['instrument']}. "
          f"Занятый риск портфеля: {load_portfolio()['current_heat_pct']}% (лимит {MAX_HEAT_PCT}%).")


def cmd_check():
    if not load_portfolio()["open_positions"]:
        print("Открытых позиций нет."); return
    print("\nПроверяю открытые позиции по истории часовых свечей...\n")
    for _kind, text in check_all():
        print(f"  {text}")
    p = load_portfolio()
    print(f"\nОсталось открытых: {len(p['open_positions'])} | занятый риск: {p['current_heat_pct']}%")


def cmd_stats():
    rows = load_results()
    if not rows:
        print("Пока нет закрытых сделок — статистики нет."); return
    s = compute_stats(rows)
    print("\n" + "═" * 52)
    print("СТАТИСТИКА ЗАКРЫТЫХ СДЕЛОК (петля результата)")
    print("═" * 52)
    print(f"  Сделок:            {s['n']}")
    print(f"  Винрейт:           {s['win_rate']}%  ({s['wins']} прибыль / {s['losses']} убыток)")
    print(f"  Средняя прибыль:   {s['avg_win']} R   | Средний убыток: {s['avg_loss']} R")
    print(f"  Матожидание:       {s['expectancy']} R за сделку  <- главный показатель")
    print(f"  Итог по деньгам:   ${s['total_pnl']}")
    if s["expectancy"] > 0:
        print("\n  Матожидание > 0 — на этой выборке система в ПЛЮСЕ.")
    elif s["expectancy"] < 0:
        print("\n  Матожидание < 0 — на этой выборке система в МИНУСЕ.")
    else:
        print("\n  Матожидание = 0 — на этой выборке система в нуле.")
    if s["n"] < MIN_TRADES_FOR_STATS:
        print(f"  ⚠ Сделок всего {s['n']} — это ещё не статистика (нужно от {MIN_TRADES_FOR_STATS}).")
    print("  (Винрейт сам по себе обманчив — смотрите вместе с матожиданием.)")


CMDS = {"open": cmd_open, "check": cmd_check, "stats": cmd_stats}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in CMDS:
        print("Команды: python3 positions.py [open | check | stats]")
    else:
        CMDS[cmd]()
