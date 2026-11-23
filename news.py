# -*- coding: utf-8 -*-
"""
НОВОСТИ для аналитика новостей: реальные заголовки с источниками.

  - 4 бесплатные RSS-ленты (без ключей): CoinDesk, Cointelegraph, Decrypt, CryptoSlate
  - индекс страха и жадности (alternative.me)
  - по каждой монете — новости за последние 48 часов, пронумерованные [1], [2], ...
  - validate(): код проверяет ответ модели. Утверждение без ссылки на реальную
    новость из списка выбрасывается; катализатор и событийный риск без ссылки
    не засчитываются. Так модель не может «придумать» новость.

Падение одной ленты не ломает анализ: остальные работают, ошибка пишется в errors.

Проверка вручную:  python3 news.py BTC/USDT
"""

import os, re, sys, html, json, datetime, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "CryptoSlate": "https://cryptoslate.com/feed/",
}
FNG_URL = "https://api.alternative.me/fng/?limit=1"

# Публичные Telegram-каналы с новостями: читаются через веб-версию t.me/s/<канал>
# (вход в аккаунт не нужен). Список — в файле news_channels.txt, по одному имени
# канала в строке (без @ и без t.me/). Закрытые каналы и боты так не читаются.
CHANNELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_channels.txt")
TG_WEB = "https://t.me/s/{name}"
HOURS = 48
MAX_COIN_ITEMS = 8
MAX_MARKET_ITEMS = 3

KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin", "bnb chain"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink"],
    "TON": ["toncoin", "the open network", "ton blockchain", "telegram's ton"],
    "TRX": ["tron", "trx", "justin sun"],
    "DOT": ["polkadot"],
    "LTC": ["litecoin", "ltc"],
    "BCH": ["bitcoin cash", "bch"],
    "NEAR": ["near protocol"],
    "SUI": ["sui network", "sui blockchain", "sui"],
}
# русские названия — для русскоязычных Telegram-каналов
KEYWORDS_RU = {
    "BTC": ["биткоин", "биткоина", "биткоину", "биткоином", "биткойн", "биток"],
    "ETH": ["эфириум", "эфириума", "эфир", "эфира"],
    "SOL": ["солана", "соланы", "солане"],
    "XRP": ["рипл", "ripple"],
    "DOGE": ["догикоин", "доги"],
    "TON": ["тон", "тона", "toncoin"],
    "ADA": ["кардано"], "AVAX": ["аваланч"], "LINK": ["чейнлинк"], "TRX": ["трон"],
    "DOT": ["полкадот"], "LTC": ["лайткоин"], "BCH": ["биткоин кэш"], "SUI": ["суи"],
}
for _k, _v in KEYWORDS_RU.items():
    KEYWORDS.setdefault(_k, []).extend(_v)

# фразы, которые НЕ относятся к монете, хотя содержат её слово (Bitcoin Cash ≠ Bitcoin)
EXCLUDE = {"BTC": ["bitcoin cash", "wrapped bitcoin", "биткоин кэш"]}
# общие события рынка, которые бьют по всем монетам сразу
MARKET_WORDS = ["federal reserve", "fed", "fomc", "interest rate", "cpi", "inflation", "sec",
                "etf", "stablecoin", "liquidation", "crypto market", "hack", "exploit", "tariff",
                "фрс", "ставка", "ставку", "инфляция", "инфляции", "крипторынок", "криптовалюты",
                "ликвидации", "взлом", "пошлины", "регулятор"]


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fund-bot paper trading)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _clean(text, limit=300):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def parse_feed(xml_bytes, source):
    """RSS 2.0 и Atom → список новостей {title, link, published, summary, source}."""
    root = ET.fromstring(xml_bytes)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        out.append({"title": _clean(it.findtext("title"), 200), "link": (it.findtext("link") or "").strip(),
                    "published": _parse_date(it.findtext("pubDate")),
                    "summary": _clean(it.findtext("description")), "source": source})
    for it in root.iter(f"{atom}entry"):
        link = it.find(f"{atom}link")
        out.append({"title": _clean(it.findtext(f"{atom}title"), 200),
                    "link": link.get("href", "") if link is not None else "",
                    "published": _parse_date(it.findtext(f"{atom}published") or it.findtext(f"{atom}updated")),
                    "summary": _clean(it.findtext(f"{atom}summary")), "source": source})
    return [x for x in out if x["title"]]


def telegram_channels():
    if not os.path.exists(CHANNELS_PATH):
        return []
    names = []
    with open(CHANNELS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            line = re.sub(r"^(https?://)?(t\.me/)?(s/)?@?", "", line).strip("/ ")
            if re.fullmatch(r"[A-Za-z0-9_]{4,64}", line):
                names.append(line)
    return names


_POST = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)


def parse_telegram(html_bytes, channel):
    """Посты из веб-версии публичного канала t.me/s/<канал>."""
    page = html_bytes.decode("utf-8", "replace")
    out = []
    for block in _POST.findall(page):
        post = re.search(r'data-post="([^"]+)"', block)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not (post and when and body):
            continue
        text = _clean(re.sub(r"<br\s*/?>", " ", body.group(1)), 600)
        if not text:
            continue
        title = text.split(". ")[0][:200]
        out.append({"title": title, "link": f"https://t.me/{post.group(1)}", "published": _parse_date(when.group(1)),
                    "summary": text[:300], "source": f"Telegram @{channel}"})
    return out


def fetch_all(feeds=None, fetch=_get, channels=None):
    """Скачивает все ленты и Telegram-каналы один раз за запуск. Возвращает (новости, ошибки)."""
    items, errors = [], []
    for source, url in (feeds or FEEDS).items():
        try:
            items.extend(parse_feed(fetch(url), source))
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{source}: {e}")
    for name in (telegram_channels() if channels is None else channels):
        try:
            posts = parse_telegram(fetch(TG_WEB.format(name=name)), name)
            if not posts:
                errors.append(f"Telegram @{name}: постов не видно — канал закрыт, это бот или имя неверное")
            items.extend(posts)
        except Exception as e:                       # noqa: BLE001
            errors.append(f"Telegram @{name}: {e}")
    return items, errors


def fear_greed(fetch=_get):
    """Индекс страха и жадности: 0 — паника, 100 — эйфория. None, если недоступен."""
    try:
        d = json.loads(fetch(FNG_URL))["data"][0]
        return {"value": int(d["value"]), "label": d.get("value_classification", ""), "source": "alternative.me"}
    except Exception:                                # noqa: BLE001
        return None


def _matches(words, text):
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def items_for(symbol, items, now=None, hours=HOURS):
    """Новости по монете за последние часы + несколько общих новостей рынка.
    Нумерует их [1..N] — по этим номерам модель обязана ссылаться."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    fresh = [x for x in items if x["published"] and (now - x["published"]).total_seconds() <= hours * 3600]
    fresh.sort(key=lambda x: x["published"], reverse=True)
    base = symbol.split("/")[0]
    words = KEYWORDS.get(base, [base.lower()])
    excludes = EXCLUDE.get(base, [])
    seen, coin, market = set(), [], []
    for x in fresh:
        key = x["title"].lower()
        if key in seen:
            continue
        text = f"{x['title']} {x['summary']}".lower()
        coin_text = text
        for phrase in excludes:
            coin_text = coin_text.replace(phrase, " ")
        if _matches(words, coin_text):
            if len(coin) < MAX_COIN_ITEMS:       # новость про монету не уходит в «рынок»,
                coin.append(x); seen.add(key)    # даже если лимит новостей по монете набран
        elif _matches(MARKET_WORDS, text) and len(market) < MAX_MARKET_ITEMS:
            market.append(x); seen.add(key)
    picked = coin + market
    return [{"n": i + 1, "title": x["title"], "source": x["source"], "link": x["link"],
             "published": x["published"].strftime("%Y-%m-%d %H:%M UTC"), "summary": x["summary"],
             "scope": "монета" if x in coin else "рынок"}
            for i, x in enumerate(picked)]


def _valid(refs, n):
    out = []
    for r in refs or []:
        try:
            r = int(str(r).strip("[] "))
        except ValueError:
            continue
        if 1 <= r <= n and r not in out:
            out.append(r)
    return out


def validate(out, news_items):
    """Проверяет ответ аналитика новостей против выданного списка.
    Всё, что не подкреплено реальной новостью из списка, выбрасывается."""
    n = len(news_items)
    out = dict(out or {})
    notes = []

    claims, dropped = [], 0
    for c in out.get("claims") or []:
        refs = _valid(c.get("refs"), n) if isinstance(c, dict) else []
        if refs:
            claims.append({"text": c.get("text", ""), "refs": refs})
        else:
            dropped += 1
    out["claims"] = claims
    if dropped:
        notes.append(f"отброшено утверждений без источника: {dropped}")

    coin_refs = {x["n"] for x in news_items if x.get("scope") == "монета"}
    refs = _valid(out.get("catalyst_refs"), n)
    out["catalyst_refs"] = [r for r in refs if r in coin_refs]
    if out.get("has_catalyst") and not out["catalyst_refs"]:
        why = ("ссылается на общую новость рынка, а не на новость про эту монету" if refs
               else "без ссылки на новость")
        notes.append(f"катализатор «{out.get('catalyst', '')}» {why} — не засчитан")
        out["has_catalyst"] = False
        out["catalyst"] = ""

    out["event_refs"] = _valid(out.get("event_refs"), n)
    if out.get("event_risk_flag") and not out["event_refs"]:
        out["event_risk_flag"] = False
        notes.append("событийный риск без ссылки на новость — не засчитан")

    if n == 0:
        out["confidence"] = "low"
        notes.append("свежих новостей по монете нет — вывод только по настроению рынка")

    used = sorted({r for c in claims for r in c["refs"]} | set(out["catalyst_refs"]) | set(out["event_refs"]))
    out["sources"] = [{"n": x["n"], "title": x["title"], "source": x["source"], "link": x["link"],
                       "published": x["published"]} for x in news_items if x["n"] in used]
    out["validation_notes"] = notes
    return out


AD_WORDS = ["реклама", "#реклама", "erid", "промокод", "скидк", "вебинар", "курс обучения", "марафон",
            "бонус", "розыгрыш", "подписывайся", "подпишись", "переходи", "регистрируйся", "партнёр", "партнер"]
SIGNAL_WORDS = ["вход", "тейк", "take profit", "стоп-лосс", "stop loss", " tp", " sl", "x10", "x20", "плечо",
                "сигнал", "лонг", "шорт", "long", "short"]


def check_channels(names, fetch=_get):
    """Проверка кандидатов в источники: открывается ли канал, как часто пишет,
    сколько рекламы и торговых сигналов, о чём последние посты."""
    now = datetime.datetime.now(datetime.timezone.utc)
    report = []
    for name in names:
        row = {"channel": name, "ok": False}
        try:
            posts = parse_telegram(fetch(TG_WEB.format(name=name)), name)
        except Exception as e:                       # noqa: BLE001
            row["error"] = str(e)[:150]
            report.append(row); continue
        if not posts:
            row["error"] = "постов не видно — закрытый канал, бот или неверное имя"
            report.append(row); continue
        dates = [p["published"] for p in posts if p["published"]]
        texts = [p["summary"].lower() for p in posts]
        ads = sum(1 for t in texts if any(w in t for w in AD_WORDS))
        sig = sum(1 for t in texts if sum(w in f" {t}" for w in SIGNAL_WORDS) >= 2)
        coins = sum(1 for t in texts if any(_matches(ws, t) for ws in KEYWORDS.values()))
        fresh = sum(1 for d in dates if (now - d).total_seconds() <= 48 * 3600)
        row.update({"ok": True, "posts_seen": len(posts), "posts_48h": fresh,
                    "last_post": max(dates).strftime("%Y-%m-%d %H:%M") if dates else None,
                    "ads_share": round(ads / len(posts), 2), "signals_share": round(sig / len(posts), 2),
                    "crypto_share": round(coins / len(posts), 2),
                    "sample": [p["title"][:140] for p in posts[-6:]]})
        report.append(row)
    return report


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "check-channels":
    path = sys.argv[2] if len(sys.argv) > 2 else "channel_candidates.txt"
    CHANNELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    rep = check_channels(telegram_channels())
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_check.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    lines = ["📡 Проверка Telegram-каналов"]
    for r in rep:
        if not r["ok"]:
            lines.append(f"✗ @{r['channel']}: {r.get('error')}")
        else:
            lines.append(f"✓ @{r['channel']}: постов за 48 ч {r['posts_48h']}, последний {r['last_post']}, "
                         f"про крипту {int(r['crypto_share'] * 100)}%, реклама {int(r['ads_share'] * 100)}%, "
                         f"сигналы {int(r['signals_share'] * 100)}%")
    print("\n".join(lines))
    try:
        import notify
        notify.send("\n".join(lines))
    except Exception as e:                           # noqa: BLE001
        print(f"telegram: {e}")
    sys.exit(0)


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT"
    all_items, errs = fetch_all()
    for e in errs:
        print("ошибка ленты:", e)
    print("страх/жадность:", fear_greed())
    for x in items_for(sym, all_items):
        print(f"[{x['n']}] {x['published']} {x['source']} ({x['scope']}): {x['title']}\n     {x['link']}")
