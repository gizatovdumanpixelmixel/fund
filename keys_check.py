# -*- coding: utf-8 -*-
"""Проверка ключей: каждый ключ пробуется одним дешёвым запросом, итог — в Telegram.
Сами ключи нигде не печатаются.   python keys_check.py"""
import os, json, urllib.request, urllib.error

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


def main():
    lines = ["🔑 Проверка ключей",
             _bearer("GROQ_API_KEY", "https://api.groq.com/openai/v1/models"),
             _bearer("OLLAMA_API_KEY", "https://ollama.com/v1/models"),
             alphavantage()]
    text = "\n".join(lines)
    print(text)
    notify.send(text)


if __name__ == "__main__":
    main()
