# -*- coding: utf-8 -*-
"""Проверка ключей: каждый ключ пробуется одним дешёвым запросом, итог — в Telegram.
Сами ключи нигде не печатаются.   python keys_check.py"""
import os, json, urllib.request, urllib.error, urllib.parse

import notify


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "fund-bot", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _bearer(name, url):
    key = os.environ.get(name)
    if not key:
        return f"⚪ {name}: не задан"
    try:
        d = _get(url, {"Authorization": f"Bearer {key}"})
        return f"✅ {name}: работает (моделей доступно: {len(d.get('data') or d.get('models') or [])})"
    except urllib.error.HTTPError as e:
        return f"❌ {name}: ошибка {e.code} — ключ неверный или отозван"
    except Exception as e:                       # noqa: BLE001
        return f"⚠️ {name}: не удалось проверить ({type(e).__name__})"


def alphavantage():
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key:
        return "⚪ ALPHAVANTAGE_API_KEY: не задан"
    try:
        d = _get(f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=SPY&apikey={key}")
    except Exception as e:                       # noqa: BLE001
        return f"⚠️ ALPHAVANTAGE_API_KEY: не удалось проверить ({type(e).__name__})"
    q = d.get("Global Quote") or {}
    if q.get("05. price"):
        return f"✅ ALPHAVANTAGE_API_KEY: работает (SPY = {float(q['05. price']):.2f}, торговый день {q.get('07. latest trading day')})"
    msg = d.get("Error Message") or d.get("Information") or d.get("Note") or "пустой ответ"
    return f"❌ ALPHAVANTAGE_API_KEY: {msg[:200]}"


WORKER_DEFAULT = "https://fund-bot.gizatovduman.workers.dev"


def worker():
    """Доступен ли Cloudflare Worker; если в GitHub задан BOT_SECRET — заодно включает webhook (/setup)."""
    url = (os.environ.get("BOT_WORKER_URL") or WORKER_DEFAULT).rstrip("/")
    out = []
    try:
        req = urllib.request.Request(url + "/", headers={"User-Agent": "fund-bot"})
        with urllib.request.urlopen(req, timeout=20) as r:
            out.append(f"✅ Worker {url}: отвечает ({r.read().decode('utf-8')[:40]})")
    except Exception as e:                       # noqa: BLE001
        return [f"❌ Worker {url}: не отвечает ({e})"]
    if not os.environ.get("BOT_WORKER_URL"):
        out.append("⚪ BOT_WORKER_URL в GitHub не задан (шаг 8)")
    key = os.environ.get("BOT_SECRET")
    if not key:
        out.append("⚪ BOT_SECRET в GitHub не задан (шаг 8) — /setup не выполнен")
        return out
    try:
        req = urllib.request.Request(url + "/setup?key=" + urllib.parse.quote(key), headers={"User-Agent": "fund-bot"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        hook = d.get("webhook", {})
        out.append(("✅ Мгновенный режим включён" if hook.get("ok") else f"❌ webhook: {hook.get('description')}")
                   + f"; доступ Worker к GitHub: {d.get('github')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        out.append("❌ /setup: пароль BOT_SECRET в GitHub и в Cloudflare не совпадает" if e.code == 403
                   else f"❌ /setup: {e.code} {body}")
    except Exception as e:                       # noqa: BLE001
        out.append(f"⚠️ /setup: {e}")
    return out


def main():
    lines = ["🔑 Проверка ключей",
             _bearer("GROQ_API_KEY", "https://api.groq.com/openai/v1/models"),
             _bearer("OLLAMA_API_KEY", "https://ollama.com/v1/models"),
             alphavantage()] + worker()
    text = "\n".join(lines)
    print(text)
    notify.send(text)


if __name__ == "__main__":
    main()
