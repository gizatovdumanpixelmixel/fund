# -*- coding: utf-8 -*-
"""
TELEGRAM: отправка сообщений и чтение команд через бота.

Боту не нужен свой сервер: облако по расписанию само забирает новые команды
(getUpdates) и отправляет ответы (sendMessage).

  TELEGRAM_TOKEN    — токен от @BotFather (в GitHub: Settings → Secrets → Actions)
  TELEGRAM_CHAT_ID  — необязательно. Если не задан, бот запомнит первый личный чат,
                      который нажал Start, и дальше слушает ТОЛЬКО его.
Состояние (чат и номер последнего прочитанного сообщения) — в telegram_state.json.
"""

import os, json, urllib.request, urllib.parse, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "telegram_state.json")
API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3900


def token():
    return os.environ.get("TELEGRAM_TOKEN", "").strip()


def enabled():
    return bool(token())


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"chat_id": None, "offset": 0}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _call(method, params):
    url = API.format(token=token(), method=method)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        return json.loads(r.read())


def chat_id():
    env = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return int(env) if env else load_state().get("chat_id")


def get_updates():
    """Новые сообщения боту. Первый личный чат запоминается как владелец."""
    st = load_state()
    res = _call("getUpdates", {"offset": st.get("offset", 0), "timeout": 0})
    updates = res.get("result", [])
    out = []
    for u in updates:
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            continue
        if not st.get("chat_id") and not os.environ.get("TELEGRAM_CHAT_ID"):
            st["chat_id"] = chat["id"]               # владелец — первый, кто написал боту
        out.append({"chat_id": chat.get("id"), "text": (msg.get("text") or "").strip(),
                    "date": msg.get("date")})
    save_state(st)
    owner = chat_id()
    return [m for m in out if m["chat_id"] == owner]


def send(text):
    """Отправить сообщение владельцу. Длинный текст режется на части."""
    if not enabled():
        print("[telegram выключен] " + text[:300])
        return False
    cid = chat_id()
    if not cid:
        get_updates()                                # вдруг владелец уже нажал Start
        cid = chat_id()
    if not cid:
        print("[telegram] чат неизвестен: откройте бота и нажмите Start")
        return False
    parts, chunk = [], ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > LIMIT:
            parts.append(chunk); chunk = ""
        chunk += line + "\n"
    parts.append(chunk)
    for part in parts:
        if part.strip():
            _call("sendMessage", {"chat_id": cid, "text": part, "disable_web_page_preview": "true"})
    return True


# ── форматирование ───────────────────────────────────────────────────────────

SIDE = {"long": "ЛОНГ (покупка)", "short": "ШОРТ (продажа)"}
SENT = {"positive": "🟢", "negative": "🔴", "neutral": "⚪", "mixed": "🟡"}


def format_briefing(data):
    b, p = data.get("briefing") or {}, data.get("package") or {}
    sid = p.get("sprint_id", "")
    try:
        when = datetime.datetime.strptime(sid, "%Y%m%d_%H%M%S").strftime("%d.%m %H:%M")
    except ValueError:
        when = sid
    lines = [f"📊 Анализ {when}"]
    reg = (p.get("market_regime") or {}).get("note")
    fng = p.get("fear_greed") or {}
    if reg:
        lines.append(reg[:1].upper() + reg[1:])
    if fng:
        lines.append(f"Индекс страха/жадности: {fng.get('value')} ({fng.get('label', '')})")
    if p.get("direction") and p["direction"] != "any":
        lines.append(f"Направление запуска: {p['direction']}")
    if p.get("model_limit_note"):
        lines.append(f"⚠ {p['model_limit_note'][:1].upper()}{p['model_limit_note'][1:]}")
    if p.get("direction_note"):
        lines.append(f"⚠ {p['direction_note'].capitalize()}")
    lines.append("")

    debates = {o.get("id"): o for o in ((data.get("report_2") or {}).get("clean_options") or [])}
    plain = {c.get("id"): c for c in b.get("candidates_plain", []) if isinstance(c, dict)}
    cands = p.get("candidates") or []
    if cands:
        lines.append(f"✅ Идеи для входа: {len(cands)}")
        for c in cands:
            pl = plain.get(c["id"], {})
            mode = " · 🔄 отскок, риск вполовину" if c.get("mode") == "bounce" else ""
            lines.append(f"\n{c['id']} · {c['instrument']} · {SIDE.get(c['direction'], c['direction'])}{mode}")
            lines.append(f"Вход ~{c['entry']} · стоп {c['stop']} · цель {c.get('target_far')}")
            lines.append(f"Риск ${c.get('risk_amount')} ({c.get('risk_pct')}%) · R после издержек {c.get('expected_R_net')}")
            if pl.get("why"):
                lines.append(f"Почему: {pl['why']}")
            if pl.get("what_could_go_wrong"):
                lines.append(f"Риск: {pl['what_could_go_wrong']}")
            d = debates.get(c["id"])
            if d:
                lines.append(f"Судья: победила сторона «{'лонг' if d.get('winner') == 'long' else 'шорт'}» — {d.get('reason', '')}")
            for n in c.get("notes", []):
                lines.append(f"ⓘ {n}")
            lines.append(f"Записать вход: /open {c['id']}")
    else:
        lines.append("Сделок нет.")
        rejected = p.get("rejected_candidates") or []
        if not rejected:                 # причины по монетам — только если не было снятых кандидатов
            reason = p.get("no_trade_reason") or b.get("summary", "")
            for part in [x.strip() for x in str(reason).split(" | ") if x.strip()][:8]:
                lines.append(f"• {part}")
    for x in p.get("rejected_candidates", []):
        side = "шорт" if x.get("direction") == "short" else "лонг"
        lines.append(f"\n✗ {x['instrument']} {side} · вход ~{x.get('entry')} · стоп {x.get('stop')} · "
                     f"цель {x.get('target_far')} · R {x.get('expected_R_net')}")
        reasons = [r.strip() for r in str(x.get("reason", "")).split(";") if r.strip()]
        lines.append(f"Снял {x['rejected_by']}:")
        for r in reasons[:4]:
            lines.append(f"  – {r}")

    news = p.get("market_news") or {}
    if news:
        lines.append("\n📰 Новости и настроение")
        for sym, v in news.items():
            head = f"{SENT.get(v.get('sentiment'), '⚪')} {sym}"
            bits = []
            if v.get("has_catalyst"):
                bits.append(f"катализатор: {v['catalyst']}")
            if v.get("event_risk"):
                bits.append(f"⚠ {v['event_risk']}")
            if not bits:
                sent = {"positive": "позитивное", "negative": "негативное", "neutral": "нейтральное",
                        "mixed": "смешанное"}.get(v.get("sentiment"), "не определено")
                bits.append(f"настроение {sent}, явного катализатора нет (новостей: {v.get('news_found', 0)})")
            lines.append(head + " — " + "; ".join(bits))

    u = p.get("llm_usage") or {}
    if u.get("calls"):
        lines.append(f"\n⚙ Вызовов модели: {u['calls']}, токенов: {u['tokens_in'] + u['tokens_out']:,}"
                     f" (на платном тарифе ≈ ${u['cost_usd_if_paid']})".replace(",", " "))
    return "\n".join(lines)


def _money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


def format_monitor(closed, rows, only_alerts=True):
    """Сообщение монитора: вердикт, где позиция, риски, сценарии, что делать."""
    lines = []
    for _kind, text in closed:
        lines.append(f"🔔 Закрыта: {text}")
    for r in rows:
        if only_alerts and r["verdict"] == "HOLD":
            continue
        icon = {"CLOSE": "🛑", "REVIEW": "⚠️", "HOLD": "✅"}.get(r["verdict"], "•")
        side = {"long": "лонг", "short": "шорт"}.get(r.get("direction"), r.get("direction"))
        mode = " · отскок" if r.get("mode") == "bounce" else ""
        if lines:
            lines.append("")
        lines.append(f"{icon} {r['verdict_ru']}: {r['id']} {r['instrument']} {side}{mode}")
        if r.get("price") is None:
            lines += [f"   – {x}" for x in r["reasons"]]
            continue
        lines.append(f"Сейчас: цена {r['price']} (вход {r['entry']}, {r['move_from_entry_pct']:+}%), "
                     f"результат {r['progress_R']:+} R = {_money(r['pnl_usd'])}, в сделке {r['days_open']} дн.")
        trend_ru = {"up": "вверх", "down": "вниз", "range": "боковик"}
        lines.append(f"Фон: тренд монеты 4ч — {trend_ru.get(r['trend'], r['trend'])}, "
                     f"режим BTC — {trend_ru.get(r['regime'], r['regime'])}")
        if r.get("risks"):
            lines.append("")
            lines.append("Риски:")
            for title, detail in r["risks"]:
                lines.append(f"• {title}: {detail}")
        sc = r.get("scenarios") or {}
        if sc:
            lines.append("")
            lines.append("Сценарии (расчёт кода от текущей цены):")
            for t in sc.get("targets", []):
                lines.append(f"📈 Цель {t['price']} ({t['move_pct']:+}%): итог {t['result_R']:+} R = "
                             f"{_money(t['result_usd'])}" + (f"; {t['eta']}" if t.get("eta") else ""))
            s = sc["stop"]
            lines.append(f"📉 Стоп {s['price']} ({s['move_pct']:+}%): итог −1 R = {_money(s['result_usd'])}, "
                         f"от текущего {_money(s['giveback_usd'])}" + (f"; {s['eta']}" if s.get("eta") else ""))
        fc = r.get("forecast")
        if fc:
            lines.append("")
            lines.append("Прогноз (мнение модели, не расчёт):")
            for key, label in (("base", "Базовый"), ("up", "Вверх"), ("down", "Вниз"), ("key_level", "Ключевой уровень")):
                if fc.get(key):
                    lines.append(f"• {label}: {fc[key]}")
        lines.append("")
        lines.append(f"👉 Что делать: {r['action']}")
    return "\n".join(lines)