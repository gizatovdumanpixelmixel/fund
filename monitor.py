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

import sys, time, datetime, subprocess

import positions as P
import context as C
import derivatives as D

HOLD_MAX_DAYS = 15
NEAR_STOP_R = 0.25
BREAKEVEN_R = 1.0
WATCH_EVERY_MIN = 60

VERDICT_RU = {"CLOSE": "ЗАКРЫТЬ", "REVIEW": "ПЕРЕСМОТРЕТЬ", "HOLD": "ДЕРЖАТЬ"}


def _trend_4h(symbol):
    o = P.exchange().fetch_ohlcv(symbol, timeframe="4h", limit=40)
    closes = [c[4] for c in o]
    fast, slow, last = C._sma(closes, 10), C._sma(closes, 30), closes[-1]
    if fast > slow and last >= slow:
        return "up"
    if fast < slow and last <= slow:
        return "down"
    return "range"


def assess(pos, price, trend, regime, positioning=None, now=None):
    """Чистая логика вердикта (без сети). Возвращает dict."""
    now = now or datetime.datetime.now()
    d, entry, stop = pos["direction"], pos["entry"], pos["stop"]
    risk_unit = abs(entry - stop) or 1e-12
    progress_R = round(((price - entry) if d == "long" else (entry - price)) / risk_unit, 2)
    to_stop_R = round(abs(price - stop) / risk_unit, 2)
    try:
        days = (now - datetime.datetime.fromisoformat(pos["opened_at"])).days
    except (KeyError, ValueError):
        days = 0
    against = {"long": "down", "short": "up"}[d]

    close, review = [], []
    if trend == against and progress_R < 0:
        close.append(f"тренд монеты на 4ч развернулся против позиции ({C.REGIME_RU[trend]}), "
                     f"цена хуже входа ({progress_R} R)")
    if regime == against and progress_R < 0:
        close.append(f"режим рынка BTC развернулся против позиции ({C.REGIME_RU[regime]}), "
                     f"цена хуже входа ({progress_R} R)")
    if to_stop_R <= NEAR_STOP_R:
        review.append(f"до стопа осталось {to_stop_R} R")
    if days > HOLD_MAX_DAYS:
        review.append(f"позиция открыта {days} дн. — срок свинга (3–15 дней) вышел")
    if progress_R >= BREAKEVEN_R:
        review.append(f"прибыль уже {progress_R} R — можно перенести стоп в безубыток "
                      f"(на цену входа {entry})")
    warn = D.warning_for(d, positioning)
    if warn:
        review.append(warn)

    verdict = "CLOSE" if close else ("REVIEW" if review else "HOLD")
    return {"id": pos["id"], "instrument": pos["instrument"], "direction": d, "price": price,
            "progress_R": progress_R, "to_stop_R": to_stop_R, "days_open": days,
            "verdict": verdict, "verdict_ru": VERDICT_RU[verdict], "reasons": close + review}


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
            trend = _trend_4h(pos["instrument"])
        except Exception as e:                       # noqa: BLE001
            out.append({"id": pos["id"], "instrument": pos["instrument"], "verdict": "REVIEW",
                        "verdict_ru": "ПЕРЕСМОТРЕТЬ", "reasons": [f"нет данных рынка: {e}"],
                        "direction": pos["direction"], "price": None, "progress_R": None,
                        "to_stop_R": None, "days_open": None})
            continue
        positioning = D.positioning(perp, pos["instrument"])
        out.append(assess(pos, price, trend, regime, positioning))
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
