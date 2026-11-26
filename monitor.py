# -*- coding: utf-8 -*-
"""
МОНИТОР ПОРТФЕЛЯ: держать, пересмотреть или закрыть.

Вердикт ставит КОД по правилам — модель здесь не нужна и не участвует:
  ЗАКРЫТЬ      — тренд монеты на 4ч развернулся против позиции и цена уже хуже входа;
                 или режим рынка BTC развернулся против позиции и цена хуже входа
  ПЕРЕСМОТРЕТЬ — цена ближе 0.25R к стопу; позиция дольше 15 дней (срок свинга вышел);
                 прибыль уже от +1R (подумать о переносе стопа в безубыток);
                 толпа на фьючерсах стоит в ту же сторону
  ДЕРЖАТЬ      — ничего из этого

Сначала монитор закрывает позиции, задевшие стоп или цель (по истории свечей),
потом оценивает оставшиеся. Позиции и стопы он не меняет — только советует.

Запуск:
  python3 monitor.py            — одна проверка
  python3 monitor.py watch      — проверять раз в час, пока Mac включён (Ctrl+C — стоп)
  python3 monitor.py recheck    — перед входом: проверить кандидатов последнего отчёта
                                  по текущей цене (ничего не записывает)
"""

import re, sys, time, datetime, subprocess

import positions as P
import context as C
import derivatives as D

HOLD_MAX_DAYS = 15
NEAR_STOP_R = 0.25
BREAKEVEN_R = 1.0
WATCH_EVERY_MIN = 60

VERDICT_RU = {"CLOSE": "ЗАКРЫТЬ", "REVIEW": "ПЕРЕСМОТРЕТЬ", "HOLD": "ДЕРЖАТЬ"}


def _trend_4h(symbol):
    """Тренд монеты на 4ч и средний размах 4ч-свечи (для оценки расстояний во времени)."""
    o = P.exchange().fetch_ohlcv(symbol, timeframe="4h", limit=40)
    closes = [c[4] for c in o]
    fast, slow, last = C._sma(closes, 10), C._sma(closes, 30), closes[-1]
    rng = [c[2] - c[3] for c in o[-14:]]
    avg_range = sum(rng) / len(rng) if rng else None
    recent = o[-20:]                                 # ~3 суток 4ч-свечей
    dec = max(len(repr(float(x)).split(".")[1].rstrip("0")) for c in recent for x in (c[2], c[3]))
    levels = {"resistance_3d": max(c[2] for c in recent), "support_3d": min(c[3] for c in recent),
              "sma_4h_30": round(slow, dec)}
    trend = "up" if (fast > slow and last >= slow) else ("down" if (fast < slow and last <= slow) else "range")
    return trend, avg_range, levels


def _pct(a, b):
    return round((a / b - 1) * 100, 2)


def _eta(dist, avg_range):
    """Сколько обычных 4ч-свечей укладывается в расстояние. Это мера дальности, не прогноз направления."""
    if not avg_range:
        return None
    n = dist / avg_range
    return f"≈{n:.1f} обычн. 4ч-свечи (~{n * 4:.0f} ч при прямом движении)"


def assess(pos, price, trend, regime, positioning=None, now=None, avg_range=None):
    """Чистая логика вердикта (без сети). Возвращает dict с вердиктом, причинами, рисками и сценариями."""
    now = now or datetime.datetime.now()
    d, entry, stop = pos["direction"], pos["entry"], pos["stop"]
    sign = 1 if d == "long" else -1
    risk_unit = abs(entry - stop) or 1e-12
    risk_usd = pos.get("risk_amount") or 0
    progress_R = round(sign * (price - entry) / risk_unit, 2)
    to_stop_R = round(abs(price - stop) / risk_unit, 2)
    try:
        days = (now - datetime.datetime.fromisoformat(pos["opened_at"])).days
    except (KeyError, ValueError):
        days = 0
    against = {"long": "down", "short": "up"}[d]
    size = (pos.get("size") or {}).get("value")
    notional = round(size * price, 2) if size else None

    close, review, kinds = [], [], []
    if trend == against and progress_R < 0:
        close.append(f"тренд монеты на 4ч развернулся против позиции ({C.REGIME_RU[trend]}), "
                     f"цена хуже входа ({progress_R} R)")
        kinds.append("trend_against")
    if regime == against and progress_R < 0:
        close.append(f"режим рынка BTC развернулся против позиции ({C.REGIME_RU[regime]}), "
                     f"цена хуже входа ({progress_R} R)")
        kinds.append("regime_against")
    if to_stop_R <= NEAR_STOP_R:
        review.append(f"до стопа осталось {to_stop_R} R")
        kinds.append("near_stop")
    if days > HOLD_MAX_DAYS:
        review.append(f"позиция открыта {days} дн. — срок свинга (3–15 дней) вышел")
        kinds.append("too_long")
    if progress_R >= BREAKEVEN_R:
        review.append(f"прибыль уже {progress_R} R — можно перенести стоп в безубыток "
                      f"(на цену входа {entry})")
        kinds.append("breakeven")
    warn = D.warning_for(d, positioning, notional)
    if warn:
        review.append(warn)
        kinds.append("crowd")

    verdict = "CLOSE" if close else ("REVIEW" if review else "HOLD")

    # ── Сценарии: всё считает код, от текущей цены ──
    raw_targets = pos.get("targets") or ([pos["target_far"]] if pos.get("target_far") else [])
    targets = sorted(raw_targets, key=lambda t: sign * t)
    scen = {"stop": {"price": stop, "move_pct": _pct(stop, price), "result_R": -1.0,
                     "result_usd": round(-risk_usd, 2), "giveback_usd": round(-(progress_R + 1) * risk_usd, 2),
                     "eta": _eta(abs(price - stop), avg_range)},
            "targets": []}
    for t in targets:
        r = round(sign * (t - entry) / risk_unit, 2)
        scen["targets"].append({"price": t, "move_pct": _pct(t, price), "result_R": r,
                                "result_usd": round(r * risk_usd, 2), "eta": _eta(abs(t - price), avg_range)})

    # ── Риски с объяснениями ──
    risks = []
    if "crowd" in kinds:
        what = ("при небольшом снижении чужие лонги начнут закрываться принудительно и ускорят падение"
                if d == "long" else
                "при небольшом росте чужие шорты начнут закрываться принудительно и ускорят рост (шорт-сквиз)")
        risks.append(("Толпа на фьючерсах стоит туда же, куда и ты", what + "\n"
                      + "\n".join(f"   ◦ {x}" for x in D.crowd_details(d, positioning, notional))))
    if trend == against:
        risks.append(("Тренд монеты против тебя", f"4ч-тренд {C.REGIME_RU[trend]}. Пока цена выше входа — "
                      "это предупреждение; если уйдёт хуже входа, монитор скажет ЗАКРЫТЬ"))
    elif trend == "range":
        risks.append(("Тренд не подтверждён", "4ч-тренд боковой: импульса в твою сторону сейчас нет, "
                      "цена может долго топтаться между стопом и целью"))
    if regime == against:
        risks.append(("Рынок против тебя", f"режим BTC {C.REGIME_RU[regime]}"
                      + (" — сделка открыта в режиме «отскок» с половинным риском, это ожидаемо, "
                         "но движение BTC может утянуть монету" if pos.get("mode") == "bounce" else "")))
    if "near_stop" in kinds:
        risks.append(("Цена у стопа", f"до стопа {scen['stop']['move_pct']}%; стоп не отодвигай"))
    if notional and pos.get("risk_pct"):
        risks.append(("Резкое движение", f"при гэпе или ликвидационном каскаде исполнение может пройти "
                      f"хуже стопа {stop}: убыток немного больше ${risk_usd:.2f}"))

    # ── Что делать (правило, не мнение модели) ──
    if verdict == "CLOSE":
        action = f"закрыть позицию: /close {pos['id']}"
    elif "near_stop" in kinds:
        action = "стоп не двигать. Либо держать до стопа по плану, либо выйти раньше вручную — решение твоё"
    elif "breakeven" in kinds:
        action = f"перенести стоп на цену входа {entry}: сделка станет безубыточной"
    elif "too_long" in kinds:
        action = "срок вышел: закрыть или осознанно продлить, записав почему"
    elif "crowd" in kinds:
        action = (f"держать по плану, стоп {stop} не отодвигать. Сигнал к выходу: 4ч-тренд развернётся "
                  f"{ {'down': 'вниз', 'up': 'вверх'}[against] } при цене хуже входа {entry} — монитор пришлёт ЗАКРЫТЬ")
    else:
        action = "держать по плану"

    return {"id": pos["id"], "instrument": pos["instrument"], "direction": d, "price": price,
            "entry": entry, "stop": stop, "mode": pos.get("mode"),
            "progress_R": progress_R, "pnl_usd": round(progress_R * risk_usd, 2),
            "move_from_entry_pct": round(sign * _pct(price, entry), 2),
            "to_stop_R": to_stop_R, "days_open": days, "trend": trend, "regime": regime,
            "verdict": verdict, "verdict_ru": VERDICT_RU[verdict], "reasons": close + review,
            "kinds": kinds, "risks": risks, "scenarios": scen, "action": action, "forecast": None}


FORECAST_SYSTEM = (
    "Ты — рыночный аналитик монитора портфеля. Вердикт по позиции уже поставлен правилами кода, "
    "ты его НЕ меняешь и НЕ даёшь советов по управлению позицией (перенос стопа, частичное закрытие, "
    "докупка) — это делают правила. Твоя задача — описать, что может сделать РЫНОК в ближайшие 1–3 дня.\n"
    "Факты, которые нельзя искажать:\n"
    "- стоп срабатывает ТОЛЬКО при цене stop; цена ниже входа — это просто убыток, а не стоп;\n"
    "- entry — цена входа Думана, а не уровень рынка: не называй её поддержкой или сопротивлением;\n"
    "- поддержка/сопротивление — ТОЛЬКО из levels (support_3d, resistance_3d, sma_4h_30);\n"
    "- коррекция = откат против тренда; рост к цели лонга — не коррекция;\n"
    "- если цель совпадает с уровнем из levels (разница до 0.3%), скажи это один раз: «цель 1 = сопротивление», "
    "а не описывай как два разных уровня.\n"
    "Используй ТОЛЬКО числа из данных. Не давай вероятностей в процентах. "
    "Если для вывода данных мало — так и скажи. Пиши по-русски, просто.\n"
    'Верни JSON: {"base": "что вероятнее всего сделает цена и почему (тренд, режим BTC, уровни), 1–2 предложения", '
    '"up": "при каком условии (цена и уровень) цена пойдёт к целям, сколько это R", '
    '"down": "при каком условии цена пойдёт к стопу, какой уровень из levels встретится по пути", '
    '"key_level": "один уровень из levels, пробой которого меняет картину, и почему"}'
)

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(obj):
    """Все числа из данных — чтобы проверить, что модель не выдумала уровни."""
    if isinstance(obj, bool) or obj is None:
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _numbers(v)]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _numbers(v)]
    if isinstance(obj, str):
        return [float(m.replace(",", ".")) for m in _NUM.findall(obj)]
    return []


def invented_levels(text, data, tol=0.003):
    """Числа в тексте модели, которых нет в данных (с допуском 0.3%). Мелкие целые (1–3 дня и т.п.) не считаем."""
    allowed = [abs(x) for x in _numbers(data)]
    bad = []
    for m in _NUM.findall(text):
        x = float(m.replace(",", "."))
        if x == int(x) and x <= 30:
            continue
        if not any(abs(x - a) <= max(a, 1e-9) * tol for a in allowed):
            bad.append(m)
    return bad


def add_forecast(row):
    """Сценарии словами от модели. Фразы с выдуманными уровнями отбрасываются кодом."""
    if row.get("price") is None:
        return row
    import json
    import llm
    keys = ("instrument", "direction", "mode", "price", "entry", "stop", "progress_R", "pnl_usd", "days_open",
            "trend", "regime", "levels", "avg_range_4h", "positioning", "reasons", "scenarios")
    data = {k: row.get(k) for k in keys}
    try:
        text, _model = llm.complete("monitor", [{"role": "system", "content": FORECAST_SYSTEM},
                                                {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                                    max_tokens=2500)
        fc = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:                           # noqa: BLE001
        print(f"  прогноз модели недоступен: {e}")
        return row
    out, dropped = {}, []
    for k in ("base", "up", "down", "key_level"):
        v = str(fc.get(k) or "").strip()
        if not v:
            continue
        bad = invented_levels(v, data)
        if bad:
            dropped.append(f"{k}: {', '.join(bad)}")
        else:
            out[k] = v
    if dropped:
        out["dropped"] = "часть прогноза отброшена — уровни не из данных: " + "; ".join(dropped)
    row["forecast"] = out or None
    return row


def alert_filter(rows, sent, now=None, repeat_h=24):
    """Какие оценки слать в Telegram. Одно и то же предупреждение — не чаще раза в repeat_h часов.
    sent: {id: {"sig": ..., "ts": iso}} — хранится в control.json. Возвращает (к отправке, новый sent)."""
    now = now or datetime.datetime.now()
    keep, new_sent = [], {}
    for r in rows:
        if r["verdict"] == "HOLD":
            continue
        sig = r["verdict"] + ":" + ",".join(sorted(r.get("kinds") or r["reasons"]))
        prev = sent.get(r["id"])
        fresh = (not prev or prev.get("sig") != sig or r["verdict"] == "CLOSE"
                 or now - datetime.datetime.fromisoformat(prev["ts"]) >= datetime.timedelta(hours=repeat_h))
        if fresh:
            keep.append(r)
            new_sent[r["id"]] = {"sig": sig, "ts": now.isoformat(timespec="seconds")}
        else:
            new_sent[r["id"]] = prev
    return keep, new_sent


def run_once():
    """Закрывает сработавшие позиции, оценивает остальные. Возвращает (закрытия, оценки)."""
    closed = [m for m in P.check_all() if m[0] in ("success", "warning")]
    regime = C.market_regime(P.exchange()).get("regime")
    import market
    perp = market.perp()
    out = []
    for pos in P.load_portfolio()["open_positions"]:
        try:
            price = P.price_now(pos["instrument"])
            trend, avg_range, levels = _trend_4h(pos["instrument"])
        except Exception as e:                       # noqa: BLE001
            out.append({"id": pos["id"], "instrument": pos["instrument"], "verdict": "REVIEW",
                        "verdict_ru": "ПЕРЕСМОТРЕТЬ", "reasons": [f"нет данных рынка: {e}"],
                        "kinds": ["no_data"], "direction": pos["direction"], "price": None,
                        "progress_R": None, "to_stop_R": None, "days_open": None})
            continue
        positioning = D.positioning(perp, pos["instrument"])
        row = assess(pos, price, trend, regime, positioning, avg_range=avg_range)
        row["levels"] = levels
        row["avg_range_4h"] = round(avg_range, 6) if avg_range else None
        row["positioning"] = {k: positioning.get(k) for k in ("funding_rate", "long_short_ratio",
                                                             "oi_change_24h_pct")} if positioning else None
        out.append(row)
    return closed, out


def recheck():
    """Проверка кандидатов последнего отчёта перед входом. Ничего не записывает."""
    data = P.latest_briefing()
    cands = (data or {}).get("package", {}).get("candidates") or []
    portfolio = P.load_portfolio()
    rows = []
    for c in cands:
        try:
            price = P.price_now(c["instrument"])
        except Exception as e:                       # noqa: BLE001
            rows.append({"instrument": c["instrument"], "ok": False, "reasons": [f"нет цены: {e}"], "notes": []})
            continue
        chk = P.preflight(c, portfolio, price)
        rows.append(dict(chk, instrument=c["instrument"], price=price))
    return rows


def notify(title, text):
    """Уведомление в углу экрана macOS. На других системах — тихо ничего не делает."""
    if sys.platform != "darwin":
        return
    safe = lambda s: s.replace('"', "'")[:200]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{safe(text)}" with title "{safe(title)}"'],
                       check=False, timeout=5)
    except Exception:                                # noqa: BLE001
        pass


def _print(closed, rows):
    stamp = datetime.datetime.now().strftime("%d.%m %H:%M")
    print(f"\n── Монитор портфеля, {stamp} ──")
    for _kind, text in closed:
        print(f"  ✓ закрыта: {text}")
    if not rows:
        print("  Открытых позиций нет.")
    for r in rows:
        side = C.DIR_RU.get(r["direction"], r["direction"])
        head = f"  {r['verdict_ru']:13} {r['id']} {r['instrument']} {side}"
        if r.get("price") is not None:
            head += f" | цена {r['price']}, результат {r['progress_R']} R, до стопа {r['to_stop_R']} R, {r['days_open']} дн."
        print(head)
        for reason in r["reasons"]:
            print(f"      – {reason}")


def _alert(closed, rows):
    for _kind, text in closed:
        notify("Фонд: позиция закрыта", text)
    for r in rows:
        if r["verdict"] != "HOLD":
            notify(f"Фонд: {r['verdict_ru']} {r['instrument']}", "; ".join(r["reasons"]))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "recheck":
        rows = recheck()
        if not rows:
            print("В последнем отчёте нет кандидатов.")
        for r in rows:
            mark = "✓ можно входить" if r["ok"] else "✗ не входить"
            print(f"  {r['instrument']}: {mark}" + (f" (цена {r.get('price')}, чистая R {r.get('R_net')})" if r["ok"] else ""))
            for x in r.get("reasons", []) + r.get("notes", []):
                print(f"      – {x}")
    elif cmd == "watch":
        print(f"Наблюдаю за портфелем раз в {WATCH_EVERY_MIN} мин. Остановить: Ctrl+C.")
        while True:
            try:
                c, r = run_once()
                _print(c, r)
                _alert(c, r)
            except Exception as e:                   # noqa: BLE001
                print(f"  ! проверка не удалась: {e}")
            time.sleep(WATCH_EVERY_MIN * 60)
    elif cmd == "":
        c, r = run_once()
        _print(c, r)
    else:
        print("Команды: python3 monitor.py [watch | recheck]")
