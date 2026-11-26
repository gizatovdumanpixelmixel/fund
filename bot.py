# -*- coding: utf-8 -*-
"""
УПРАВЛЕНИЕ ЧЕРЕЗ TELEGRAM + ЕЖЕЧАСНЫЙ ЦИКЛ В ОБЛАКЕ.

Облако запускает `python bot.py cycle` раз в час. За один цикл:
  1. читает новые команды боту и выполняет их;
  2. если режим «работать» и прошло 2 часа с прошлой проверки — монитор позиций,
     в Telegram только то, что требует внимания (закрытия, ПЕРЕСМОТРЕТЬ, ЗАКРЫТЬ).

Команды (в мгновенном режиме через Cloudflare Worker — ответ за 1–2 минуты):
  /work            — работать по расписанию
  /rest            — отдыхать (расписание пропускается; команды работают)
  /status          — режим, открытые позиции, расход за месяц
  /run [long|short] — анализ сейчас (по умолчанию любое направление)
  /check           — монитор позиций сейчас
  /open C-0001     — записать вход в идею из последнего отчёта (с проверкой по цене)
  /close P-0004    — закрыть позицию вручную по текущей цене
  /week            — сводка за неделю (приходит и сама по понедельникам)
  /help            — список команд

Отдельно:  python bot.py notify-latest   — отправить последний отчёт
           python bot.py should-run      — печатает yes/no: работать ли расписанию
"""

import os, sys, json, glob, subprocess, datetime

import notify as TG
import positions as P

BASE = os.path.dirname(os.path.abspath(__file__))
CONTROL_PATH = os.path.join(BASE, "control.json")
MONITOR_EVERY_H = 2

HELP = ("Команды:\n"
        "/work — работать по расписанию\n/rest — отдыхать\n/status — состояние\n"
        "/run — анализ сейчас (/run long, /run short)\n/check — проверить позиции\n"
        "/open C-0001 — записать вход\n/close P-0001 — закрыть позицию\n/week — сводка за неделю\n\n"
        "Ответ приходит за 1–2 минуты (анализ — 4–6 минут).")


# ── режим ────────────────────────────────────────────────────────────────────

def load_control():
    if os.path.exists(CONTROL_PATH):
        with open(CONTROL_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"mode": "work"}


def save_control(c):
    c["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(CONTROL_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def working():
    return load_control().get("mode", "work") == "work"


# ── действия ─────────────────────────────────────────────────────────────────

def latest_briefing_path():
    files = sorted(glob.glob(os.path.join(BASE, "briefings", "briefing_*.json")))
    return files[-1] if files else None


def run_analysis(direction="any"):
    before = latest_briefing_path()
    proc = subprocess.run([sys.executable, os.path.join(BASE, "pipeline.py"), "--direction", direction],
                          cwd=BASE, capture_output=True, text=True, timeout=60 * 60)
    print(proc.stdout[-4000:])
    after = latest_briefing_path()
    if proc.returncode != 0 or after == before:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        return f"❌ Анализ завершился с ошибкой:\n{tail}"
    with open(after, encoding="utf-8") as f:
        return TG.format_briefing(json.load(f))


def run_monitor(only_alerts):
    import monitor as MON
    closed, rows = MON.run_once()
    c = load_control()
    if only_alerts:                                  # одно и то же предупреждение — не чаще раза в сутки
        rows, c["monitor_alerts"] = MON.alert_filter(rows, c.get("monitor_alerts", {}))
    for r in rows:                                   # сценарии словами — только для того, что увидит Думан:
        if r["verdict"] != "HOLD" or not only_alerts:    # по кнопке — для всех позиций, в фоне — для тревог
            MON.add_forecast(r)
    text = TG.format_monitor(closed, rows, only_alerts=only_alerts)
    c["last_monitor"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_control(c)
    if not text and not only_alerts:
        text = "Открытых позиций нет." if not rows else "Все позиции: ДЕРЖАТЬ."
    return text


def status_text():
    c = load_control()
    p = P.load_portfolio()
    rows = P.load_results()
    s = P.compute_stats(rows)
    lines = [f"Режим: {'🟢 работаю' if c.get('mode', 'work') == 'work' else '😴 отдыхаю'}",
             f"Открытых позиций: {len(p['open_positions'])}, занятый риск {p.get('current_heat_pct', 0)}%"]
    for x in p["open_positions"]:
        lines.append(f"  {x['id']} {x['instrument']} {'лонг' if x['direction'] == 'long' else 'шорт'} "
                     f"вход {x['entry']} стоп {x['stop']} цель {x.get('target_far')}")
    lines.append(f"Закрытых сделок: {s['n']}" + (f", матожидание {s['expectancy']} R" if s["n"] else ""))
    last = latest_briefing_path()
    if last:
        stamp = os.path.basename(last)[len("briefing_"):-len(".json")]
        try:
            stamp = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass
        lines.append(f"Последний анализ: {stamp}")
    try:
        import llm
        lines.append(f"Расход модели за месяц (по платному тарифу): ${llm.month_spend()}")
    except Exception:                                # noqa: BLE001
        pass
    return "\n".join(lines)


def _since(rows, field, days):
    edge = datetime.datetime.now() - datetime.timedelta(days=days)
    out = []
    for r in rows:
        try:
            if datetime.datetime.fromisoformat(str(r.get(field))[:19]) >= edge:
                out.append(r)
        except ValueError:
            continue
    return out


def _usd(v):
    return f"${v:,.2f}".replace(",", " ")


def _money(v):
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    return sign + _usd(abs(v))


def _pct(v):
    return ("+" if v > 0 else ("−" if v < 0 else "")) + f"{abs(v):.2f}%"


def money_text(now=None):
    """Деньги бумажного счёта: капитал, деньги в позициях, итоги за неделю / месяц / 3 месяца.
    По закрытым сделкам: вложено = вход × размер, вернулось = вложено + доход,
    доход = чистый результат с издержками и funding (как в журнале сделок)."""
    now = now or datetime.datetime.now()
    p = P.load_portfolio()
    rows = P.load_results()
    cap0 = float(p.get("capital", 10000))
    realized = round(sum(r.get("pnl", 0) for r in rows), 2)
    lines = ["\n💰 Деньги (бумажный счёт)",
             f"Капитал: старт {_usd(cap0)} → сейчас {_usd(cap0 + realized)} ({_pct(realized / cap0 * 100)})"]

    inv = unreal = 0.0
    priced = True
    for x in p["open_positions"]:
        size = float((x.get("size") or {}).get("value") or 0)
        inv += x["entry"] * size
        try:
            px = P.price_now(x["instrument"])
            unreal += (px - x["entry"]) * size * (1 if x["direction"] == "long" else -1)
        except Exception:                            # noqa: BLE001
            priced = False
    if p["open_positions"]:
        u = f", незакрытый результат {_money(round(unreal, 2))}" if priced else ""
        lines.append(f"Сейчас в позициях: {_usd(inv)} ({len(p['open_positions'])} шт.){u}")
    else:
        lines.append("Сейчас в позициях: $0 — всё в деньгах")

    for label, days in (("Неделя", 7), ("Месяц", 30), ("3 месяца", 90)):
        edge = now - datetime.timedelta(days=days)
        closed = []
        for r in rows:
            try:
                if datetime.datetime.fromisoformat(str(r.get("closed_at"))[:19]) >= edge:
                    closed.append(r)
            except ValueError:
                continue
        if not closed:
            lines.append(f"📅 {label}: закрытых сделок нет")
            continue
        invested = sum(r["entry"] * float(r.get("size") or 0) for r in closed)
        net = round(sum(r.get("pnl", 0) for r in closed), 2)
        plus = round(sum(r["pnl"] for r in closed if r.get("pnl", 0) > 0), 2)
        minus = round(sum(r["pnl"] for r in closed if r.get("pnl", 0) < 0), 2)
        wins = sum(1 for r in closed if r.get("pnl", 0) > 0)
        on_inv = f", {_pct(net / invested * 100)} на вложенное" if invested else ""
        costs = round(sum(r.get("costs", 0) for r in closed), 2)
        lines.append(
            f"📅 {label}: сделок {len(closed)} (в плюс {wins}, в минус {len(closed) - wins})\n"
            f"   вложено {_usd(invested)} → вернулось {_usd(invested + net)}\n"
            f"   доход {_money(net)} ({_pct(net / cap0 * 100)} к капиталу{on_inv})\n"
            f"   заработали {_money(plus)}, сгорело {_money(minus) if minus else '$0'}")
        losers = sorted((r for r in closed if r.get("pnl", 0) < 0), key=lambda r: r["pnl"])
        for r in losers[:5]:
            side = "лонг" if r["direction"] == "long" else "шорт"
            why = {"СТОП": "стоп", "ВРУЧНУЮ": "закрыта вручную"}.get(r.get("hit"), str(r.get("hit", "")).lower())
            lines.append(f"     🔻 {r['instrument'].split('/')[0]} {side} {_money(r['pnl'])} — {why}, "
                         f"{str(r.get('closed_at', ''))[8:10]}.{str(r.get('closed_at', ''))[5:7]}")
        if len(losers) > 5:
            lines.append(f"     … и ещё {len(losers) - 5} на {_money(round(sum(r['pnl'] for r in losers[5:]), 2))}")
        if costs:
            lines.append(f"   на издержки (комиссия, спред, проскальзывание) ушло {_usd(costs)} — "
                         f"без них доход был бы {_money(round(net + costs, 2))}")
    if any(r.get("size_estimated") for r in rows):
        lines.append("   (по августовским сделкам размер восстановлен по результату — примерно)")
    return "\n".join(lines)


def weekly_text(days=7):
    """Сводка за неделю: анализы, идеи, сделки, уроки, табель агентов, расход модели."""
    import memory as MEM
    try:                                              # сначала разобрать свежие итоги по свечам
        MEM.review_closed(); MEM.review_rejected()
    except Exception as e:                            # noqa: BLE001
        print(f"разбор итогов: {e}")
    now = datetime.datetime.now()
    lines = [f"🗓 Сводка за неделю ({(now - datetime.timedelta(days=days)):%d.%m}–{now:%d.%m})"]

    briefs = []
    for path in glob.glob(os.path.join(BASE, "briefings", "briefing_*.json")):
        stamp = os.path.basename(path)[len("briefing_"):-len(".json")]
        try:
            if datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S") >= now - datetime.timedelta(days=days):
                with open(path, encoding="utf-8") as f:
                    briefs.append(json.load(f))
        except ValueError:
            continue
    ideas = sum(len((b.get("package") or {}).get("candidates") or []) for b in briefs)
    removed = sum(len((b.get("package") or {}).get("rejected_candidates") or []) for b in briefs)
    lines.append(f"\nАнализов: {len(briefs)} · идей для входа: {ideas} · снято экспертами: {removed}")

    decisions = _since(MEM.load(MEM.DECISIONS_PATH), "ts", days)
    setups = [d for d in decisions if d.get("setup_found")]
    lines.append(f"Решений по монетам: {len(decisions)}, из них с сетапом: {len(setups)}, "
                 f"прошли все проверки: {sum(1 for d in setups if d.get('passed'))}")

    p = P.load_portfolio()
    lines.append(f"\n💼 Открыто позиций: {len(p['open_positions'])}, занятый риск {p.get('current_heat_pct', 0)}%")
    for x in p["open_positions"]:
        lines.append(f"  {x['id']} {x['instrument']} {'лонг' if x['direction'] == 'long' else 'шорт'} "
                     f"вход {x['entry']} стоп {x['stop']} цель {x.get('target_far')}")
    week = _since(P.load_results(), "closed_at", days)
    allr = P.load_results()
    if week:
        sw = P.compute_stats(week)
        lines.append(f"Закрыто за неделю: {sw['n']} · в плюс {sw['wins']}, в минус {sw['losses']} · "
                     f"итог {round(sum(r['actual_R'] for r in week), 2)} R (${sw['total_pnl']})")
    else:
        lines.append("Закрытых сделок за неделю нет.")
    sa = P.compute_stats(allr)
    if sa["n"]:
        warn = f" — пока мало данных (нужно от {P.MIN_TRADES_FOR_STATS})" if sa["n"] < P.MIN_TRADES_FOR_STATS else ""
        lines.append(f"Всего сделок: {sa['n']}, винрейт {sa['win_rate']}%, матожидание {sa['expectancy']} R{warn}")
    try:                                              # какой способ выхода выгоднее — на реальных сделках
        import exits as EX
        txt = EX.summary_text(EX.backfill())
        if txt:
            lines.append(txt)
    except Exception as e:                            # noqa: BLE001
        lines.append(f"\n🎯 Способы выхода: не удалось посчитать ({e})")
    try:
        lines.append(money_text(now))
    except Exception as e:                            # noqa: BLE001
        lines.append(f"\n💰 Деньги: не удалось посчитать ({e})")

    lessons = _since(MEM.load(MEM.LESSONS_PATH), "created", days)
    if lessons:
        lines.append("\n📚 Новые уроки")
        for l in lessons[-5:]:
            lines.append(f"• {l['what_happened']}\n  → {l['rule']}")
    rej = _since(MEM.load(MEM.REJECTED_PATH), "reviewed", days)
    gate = [r for r in rej if r.get("kind", "rejected") == "rejected"]
    if gate:
        saved = sum(1 for r in gate if r["outcome"] == "would_stop")
        missed = sum(1 for r in gate if r["outcome"] == "would_target")
        lines.append(f"\n🚧 Отклонённые сетапы: гейты были правы {saved} раз, отсекли прибыльную сделку {missed} раз")
    shadow = [r for r in rej if r.get("kind") == "approved"]
    if shadow:
        win = sum(1 for r in shadow if r["outcome"] == "would_target")
        lose = sum(1 for r in shadow if r["outcome"] == "would_stop")
        lines.append(f"👻 Одобренные, но не открытые идеи: дошли бы до цели {win}, ушли бы в стоп {lose}")

    rows = [r for r in MEM.scorecard() if r["n"]]
    if rows:
        lines.append("\n🎯 Табель (кто чаще прав)")
        for r in rows:
            lines.append(f"  {r['agent']}: {r['correct']}/{r['n']}" +
                         (f" ({r['accuracy_pct']}%)" if r["accuracy_pct"] is not None else "") +
                         (f" — {r['note']}" if r["note"] else ""))

    try:
        import llm
        usage = _since(llm._load_usage(), "ts", days)
        if usage:
            toks = sum(r["tokens_in"] + r["tokens_out"] for r in usage)
            cost = round(sum(r["cost_usd_if_paid"] for r in usage), 3)
            lines.append(f"\n⚙ Модель за неделю: вызовов {len(usage)}, токенов {toks:,} "
                         f"(на платном тарифе ≈ ${cost})".replace(",", " "))
    except Exception:                                 # noqa: BLE001
        pass
    lines.append(f"\nРежим: {'🟢 работаю' if working() else '😴 отдыхаю'}")
    return "\n".join(lines)


def open_candidate(cid):
    path = latest_briefing_path()
    if not path:
        return "Отчётов пока нет."
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cands = {c["id"].upper(): c for c in (data.get("package") or {}).get("candidates", [])}
    cand = cands.get(cid.upper())
    if not cand:
        return f"В последнем отчёте нет идеи {cid}. Доступны: {', '.join(cands) or 'нет'}."
    price = P.price_now(cand["instrument"])
    chk = P.preflight(cand, P.load_portfolio(), price)
    if not chk["ok"]:
        return "✗ Не записано. Проверка по текущей цене " + str(price) + ":\n" + "\n".join(f"– {r}" for r in chk["reasons"])
    pid = P.record_position(cand, data["package"].get("sprint_id", ""), chk)
    note = ("\n" + "\n".join(chk["notes"])) if chk["notes"] else ""
    return (f"✓ Записана позиция {pid}: {cand['instrument']} по {chk['fill']}, "
            f"чистая R {chk['R_net']}, риск ${chk['risk_amount']}.{note}")


def handle(text):
    parts = text.split()
    if not parts:
        return None
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    if cmd in ("/start", "/help"):
        return "Бот фонда подключён.\n\n" + HELP
    if cmd == "/work":
        c = load_control(); c["mode"] = "work"; save_control(c)
        return "🟢 Работаю: анализ 2 раза в день, проверка позиций каждые 2 часа."
    if cmd == "/rest":
        c = load_control(); c["mode"] = "rest"; save_control(c)
        return "😴 Отдыхаю: расписание пропускается. Команды /run и /check работают."
    if cmd == "/status":
        return status_text()
    if cmd == "/run":
        direction = args[0].lower() if args and args[0].lower() in ("long", "short", "any") else "any"
        return run_analysis(direction)
    if cmd == "/check":
        return run_monitor(only_alerts=False)
    if cmd == "/week":
        return weekly_text()
    if cmd == "/open":
        return open_candidate(args[0]) if args else "Укажите идею: /open C-0001"
    if cmd == "/close":
        return P.close_manual(args[0]) if args else "Укажите позицию: /close P-0001"
    return "Не знаю такую команду.\n\n" + HELP


def worker_queue():
    """Команды из очереди Cloudflare Worker (мгновенный режим бота).
    Сначала команда, ради которой запущена задача (BOT_CMD), затем всё накопленное."""
    import urllib.request
    items, seen = [], set()
    first = os.environ.get("BOT_CMD", "").strip()
    if first:
        items.append({"text": first}); seen.add(os.environ.get("BOT_CMD_ID", ""))
    url = os.environ.get("BOT_WORKER_URL", "").rstrip("/")
    if url:
        try:
            req = urllib.request.Request(url + "/queue", headers={
                "X-Bot-Key": os.environ.get("BOT_SECRET", ""), "User-Agent": "fund-bot"})
            with urllib.request.urlopen(req, timeout=20) as r:
                for it in json.loads(r.read().decode("utf-8")).get("items", []):
                    if it.get("id") not in seen:
                        items.append(it); seen.add(it.get("id"))
        except Exception as e:                       # noqa: BLE001
            print(f"очередь worker: {e}")
    return items


def cycle():
    """Один цикл в облаке: по расписанию раз в час или сразу по команде из бота."""
    if TG.enabled():
        if os.environ.get("BOT_WORKER_URL") or os.environ.get("BOT_CMD"):
            msgs = worker_queue()                    # мгновенный режим: Telegram пишет в Worker
        else:
            try:
                msgs = TG.get_updates()              # старый режим: опрос раз в час
            except Exception as e:                   # noqa: BLE001
                print(f"telegram: {e}")
                msgs = []
        for m in msgs:
            try:
                reply = handle(m["text"])
            except Exception as e:                   # noqa: BLE001
                reply = f"❌ Ошибка: {e}"
            if reply:
                TG.send(reply)
    if not working():
        print("режим «отдыхать» — монитор пропущен")
        return
    last = load_control().get("last_monitor")
    due = True
    if last:
        try:
            due = (datetime.datetime.now() - datetime.datetime.fromisoformat(last)).total_seconds() >= MONITOR_EVERY_H * 3600 - 300
        except ValueError:
            pass
    if due and P.load_portfolio()["open_positions"]:
        text = run_monitor(only_alerts=True)
        if text:
            TG.send(text)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "cycle":
        cycle()
    elif cmd == "notify-latest":
        # --since-start: отправить, только если отчёт создан в этом запуске (а не старый)
        path = latest_briefing_path()
        if path and "--since-start" in sys.argv:
            try:
                started = datetime.datetime.fromisoformat(sys.argv[sys.argv.index("--since-start") + 1].replace("Z", "+00:00"))
                if os.path.getmtime(path) < started.timestamp() - 60:
                    print("нового отчёта нет"); sys.exit(1)
            except (ValueError, IndexError):
                pass
        if path:
            with open(path, encoding="utf-8") as f:
                TG.send(TG.format_briefing(json.load(f)))
    elif cmd == "weekly":
        TG.send(weekly_text())
    elif cmd == "should-run":
        print("yes" if working() else "no")
    else:
        print(__doc__)
