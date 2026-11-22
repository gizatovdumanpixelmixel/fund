# -*- coding: utf-8 -*-
"""
ФОНД — пайплайн фазы 1a (бумага, реальные данные).

Что делает: гоняет один спринт через всю цепочку из семи агентов и выдаёт
понятный отчёт (plain_briefing). Денег не тратит — только читает рынок и думает.

Поток:
  Discovery (мандат+цель из конфига, вы = гейт)
    -> для каждого инструмента:  Технический ‖ Новости (параллельно) -> Риск
    -> Трейдер-стратег -> package
    -> Эксперт №1 ‖ Консультант №2 (ИЗОЛИРОВАННО, не видят друг друга)
    -> Переводчик -> plain_briefing
    -> вы

Запуск (Ollama должна быть открыта):
  python3 pipeline.py
  или кнопкой в панели: двойной клик по start_panel.command
"""

import os, json, re, time, datetime, traceback
from concurrent.futures import ThreadPoolExecutor

import llm                   # модели: облако Groq/Ollama, переключение, учёт токенов
import market as MK          # данные рынка: работают и на Mac, и в облаке
import ccxt

from prompts import AGENT_PROMPTS
import context as C          # портфель, режим рынка BTC, дополнительные гейты
import news as N             # реальные новости с источниками
import memory as MEM         # журнал решений, уроки, табель агентов
import positions as P        # общие функции позиций (результаты сделок)
import derivatives as D      # позиционирование на фьючерсах
import debate as DB          # дебаты лонг ↔ шорт, консультант — судья

# ──────────────────────────────────────────────────────────────────────────
# 1. КОНФИГ  (это и есть результат Discovery, который ВЫ согласовали)
# ──────────────────────────────────────────────────────────────────────────

MANDATE = {
    "capital": 10000, "currency": "USD",
    "risk_per_trade_pct": 1.0, "max_heat_pct": 4.0,
    "daily_stop_pct": -3.0, "sprint_stop_pct": -7.0, "killswitch_pct": -18.0,
    "max_concentration_pct": 15.0, "max_leverage": 5, "min_R": 1.5,
    "allowed_classes": ["crypto"],            # на 1a начинаем с крипты
    "sprint_horizon": "swing", "wip_limit": 3,
    "min_stop_vs_candle": 0.5,   # стоп не ближе 0.5 среднего размаха свечи (иначе выбьет шумом)
    "sprint_days": 5,            # окно для «стопа спринта»: последние N дней
    "assumed_hold_days": 7,      # ориентир срока удержания для платы за финансирование
    "honor_vetoes": True,        # вето судьи и «нельзя» эксперта снимают сделку из отчёта
}

# Цель спринта под каждое направление. При запуске программа спросит, какое взять.
GOAL_BY_DIRECTION = {
    "long": ("Рынок смотрим на РОСТ. Приоритет — искать сетапы на ПОКУПКУ (лонг): "
             "восходящий тренд или откат к поддержке с подтверждением объёмом. "
             "Шорт брать только при очень сильном и явном сигнале. "
             "Выносить только сетапы A-класса. Нет подходящих — допустимый итог (no_trade)."),
    "short": ("Рынок смотрим на СНИЖЕНИЕ. Приоритет — искать сетапы на ПРОДАЖУ (шорт): "
              "нисходящий тренд или отскок к сопротивлению с подтверждением объёмом. "
              "Лонг брать только при очень сильном и явном сигнале. "
              "Выносить только сетапы A-класса. Нет подходящих — допустимый итог (no_trade)."),
    "any": ("Ищем сетапы A-класса в ЛЮБУЮ сторону — и лонг, и шорт, что даёт рынок. "
            "Направление подбирай по тренду инструмента. "
            "Нет подходящих сетапов — допустимый итог (no_trade)."),
}
SPRINT_GOAL = GOAL_BY_DIRECTION["any"]   # значение по умолчанию; при запуске уточним
CHOSEN_DIRECTION = "any"


def ask_direction():
    """Спрашивает направление анализа в начале запуска."""
    print("\nВыберите направление анализа:")
    print("  [1] long  — искать покупки (ставка на рост)")
    print("  [2] short — искать продажи (ставка на падение)")
    print("  [3] any   — любое направление, что найдёт рынок")
    choice = input("Ваш выбор (1 / 2 / 3, Enter = any): ").strip()
    return {"1": "long", "2": "short", "3": "any", "": "any"}.get(choice, "any")

# Что сканируем. Символы в формате ccxt. Больше пар = выше шанс найти сетап,
# но и дольше/дороже прогон. Убирайте/добавляйте свободно.
# 16 самых ликвидных монет. Код отсеивает большинство до вызова модели (тренд/режим),
# поэтому расширение почти не увеличивает расход модели; новости — по всем.
WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT",
             "ADA/USDT", "AVAX/USDT", "LINK/USDT", "TON/USDT", "TRX/USDT", "DOT/USDT",
             "LTC/USDT", "BCH/USDT", "NEAR/USDT", "SUI/USDT"]

# Состояние портфеля читаем из portfolio.json (его ведёт positions.py).
# Так новый спринт видит уже занятый heat от открытых сделок.
def _load_portfolio():
    default = {"capital": MANDATE["capital"], "open_positions": [],
               "current_heat_pct": 0.0, "pnl": 0.0}
    try:
        if os.path.exists("portfolio.json"):
            with open("portfolio.json", encoding="utf-8") as f:
                p = json.load(f)
            default.update({"capital": p.get("capital", default["capital"]),
                            "open_positions": p.get("open_positions", []),
                            "current_heat_pct": p.get("current_heat_pct", 0.0)})
    except Exception:                     # noqa: BLE001
        pass
    return default


PORTFOLIO_STATE = _load_portfolio()

# Подсказка по издержкам (типичные значения для крипто-перпов, можно уточнить).
COSTS_HINT = {"commission_pct": 0.04, "spread_pct": 0.02, "slippage_pct": 0.03}

# ── Провайдер ИИ ──────────────────────────────────────────────────────────
# Какие модели отвечают за какую роль — в llm.py (ROLE_CHAINS). Облако
# используется, если задан ключ GROQ_API_KEY или OLLAMA_API_KEY; иначе на Mac —
# локальная Ollama. Ниже — только для выгрузки локальной модели после прогона.
PROVIDER_BASE_URL = llm.PROVIDERS["ollama_local"]["base_url"]
DEFAULT_MODEL     = "qwen2.5:7b"

# Сколько запросов к модели одновременно. 1 — бережно к Mac: локальная модель
# не держит в памяти два контекста сразу, компьютер меньше «виснет». Для облачной
# модели можно поставить 2.
MODEL_PARALLEL = 1

MODEL_BY_AGENT = {                     # ключевым агентам можно дать модель сильнее
    "strategist": DEFAULT_MODEL,
    "capital_manager": DEFAULT_MODEL,
    "consultant": DEFAULT_MODEL,
}

EXCHANGE_ID = "binance"                 # см. market.py: в облаке — зеркало данных Binance
CANDLE_TF = "4h"
CANDLE_LIMIT = 80                       # сколько свечей отдаём аналитику

# Стартовый словарик терминов (переводчик его дополняет).
GLOSSARY = {"terms": [
    {"term": "стоп", "plain": "цена, на которой сделку закрывают с убытком, чтобы не потерять больше"},
    {"term": "R (R_net)", "plain": "во сколько раз возможная прибыль больше риска, после комиссий"},
]}

# ──────────────────────────────────────────────────────────────────────────
# 2. ЛОГИРОВАНИЕ  (каждый шаг пишем в файл — это нужно для петли прогноз/факт)
# ──────────────────────────────────────────────────────────────────────────

RUN_ID = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
llm.RUN_ID = RUN_ID
os.makedirs("logs", exist_ok=True)
os.makedirs("briefings", exist_ok=True)
LOG_PATH = f"logs/run_{RUN_ID}.jsonl"


def log_step(step, agent, model, payload_in, payload_out):
    rec = {"ts": datetime.datetime.now().isoformat(), "run_id": RUN_ID,
           "step": step, "agent": agent, "model": model,
           "input": payload_in, "output": payload_out}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ──────────────────────────────────────────────────────────────────────────
# 3. ВЫЗОВ АГЕНТА  (единый клиент для Ollama/облака + аккуратный разбор JSON)
# ──────────────────────────────────────────────────────────────────────────

def _extract_json(text):
    """Достаём JSON, даже если модель обернула его в ```json ... ```."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("В ответе модели нет JSON")
    snippet = text[start:]
    # обрезаем хвост после последней закрывающей скобки
    end = max(snippet.rfind("}"), snippet.rfind("]"))
    return json.loads(snippet[:end + 1])


LANG_RULE = ("ЯЗЫК ОТВЕТА: только русский. Китайские иероглифы и английские фразы "
             "запрещены (кроме тикеров вроде BTC/USDT и названий источников). "
             "Английские новости пересказывай по-русски.\n\n")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def _has_cjk(obj):
    return bool(_CJK.search(json.dumps(obj, ensure_ascii=False)))


def call_agent(agent_key, payload, step, max_tokens=llm.MAX_TOKENS, retries=2):
    system = LANG_RULE + AGENT_PROMPTS[agent_key]
    model = "—"
    user = json.dumps(payload, ensure_ascii=False)
    last_err = None
    fallback = None
    for attempt in range(retries + 1):
        try:
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": user}]
            if fallback is not None:
                # модель перешла на китайский — просим переписать тот же ответ по-русски
                messages += [{"role": "assistant", "content": json.dumps(fallback, ensure_ascii=False)},
                             {"role": "user", "content": "В ответе китайские иероглифы. Перепиши ТОТ ЖЕ JSON "
                                                         "полностью на русском языке, сохрани все поля и числа."}]
            text, model = llm.complete(agent_key, messages, max_tokens)   # с переключением моделей
            out = _extract_json(text)
            if _has_cjk(out) and attempt < retries:
                fallback = out
                log_step(step + "::cjk_retry", agent_key, model, payload, out)
                continue
            log_step(step, agent_key, model, payload, out)
            return out
        except llm.AllModelsFailed as e:            # все модели недоступны — повтор не поможет
            last_err = e
            break
        except Exception as e:                      # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    if fallback is not None:                        # лучше ответ с иероглифами, чем никакого
        log_step(step, agent_key, model, payload, fallback)
        return fallback
    log_step(step, agent_key, model, payload, {"error": str(last_err)})
    raise RuntimeError(f"Агент {agent_key} не дал валидный JSON: {last_err}")


# ──────────────────────────────────────────────────────────────────────────
# 4. ДАННЫЕ РЫНКА  (крипта через ccxt; публичные эндпоинты, ключи не нужны)
# ──────────────────────────────────────────────────────────────────────────

_spot = MK.spot()
_perp = "auto"                          # фьючерсная биржа выбирается при подготовке контекста


def _digest(ohlcv):
    """Считаем за модель готовую сводку: тренд, уровни, свинги, средний размах.
    Так маленькая модель не тонет в 80 сырых свечах."""
    closes = [c[4] for c in ohlcv]; highs = [c[2] for c in ohlcv]; lows = [c[3] for c in ohlcv]
    last = closes[-1]

    def sma(vals, n):
        n = min(n, len(vals)); return round(sum(vals[-n:]) / n, 2)
    sma_fast, sma_slow = sma(closes, 10), sma(closes, 30)

    rng_hi, rng_lo = max(highs), min(lows)
    range_pos = round((last - rng_lo) / (rng_hi - rng_lo) * 100, 1) if rng_hi > rng_lo else 50.0

    if sma_fast > sma_slow and last >= sma_slow:
        trend = "up"
    elif sma_fast < sma_slow and last <= sma_slow:
        trend = "down"
    else:
        trend = "range"

    recent = ohlcv[-20:]
    swing_high = max(c[2] for c in recent); swing_low = min(c[3] for c in recent)
    last14 = ohlcv[-14:]
    avg_range = round(sum(c[2] - c[3] for c in last14) / len(last14), 2)

    return {
        "timeframe": CANDLE_TF, "last_price": last, "trend": trend,
        "sma_fast_10": sma_fast, "sma_slow_30": sma_slow,
        "range_high": rng_hi, "range_low": rng_lo, "range_position_pct": range_pos,
        "swing_high_20": swing_high, "swing_low_20": swing_low,
        "avg_candle_range_14": avg_range,
        "change_pct_window": round((last / closes[0] - 1) * 100, 2),
        "recent_candles_15": ohlcv[-15:],   # короткий хвост для локальной структуры
    }


def build_market_data(symbol):
    """Компактный срез рынка: готовая сводка вместо 80 сырых свечей."""
    md = {"instrument": symbol, "asset_class": "crypto"}
    try:
        ohlcv = _spot.fetch_ohlcv(symbol, timeframe=CANDLE_TF, limit=CANDLE_LIMIT)
        ohlcv = [[c[0], round(c[1], 2), round(c[2], 2), round(c[3], 2),
                  round(c[4], 2), round(c[5], 2)] for c in ohlcv]
        md.update(_digest(ohlcv))
    except Exception as e:                # noqa: BLE001
        md["ohlcv_error"] = str(e)
    try:
        ob = _spot.fetch_order_book(symbol, limit=10)
        md["orderbook_top"] = {"bids": ob["bids"][:3], "asks": ob["asks"][:3]}
    except Exception:                     # noqa: BLE001
        pass
    if _perp not in (None, "auto"):       # funding / OI — настроения и перегрев
        try:
            fr = _perp.fetch_funding_rate(D.perp_symbol(symbol))
            md["funding_rate"] = fr.get("fundingRate")
        except Exception:                 # noqa: BLE001
            pass
        try:
            oi = _perp.fetch_open_interest(D.perp_symbol(symbol))
            md["open_interest"] = oi.get("openInterestAmount") or oi.get("openInterestValue")
        except Exception:                 # noqa: BLE001
            pass
    return md


# ──────────────────────────────────────────────────────────────────────────
# 5. РИСК-ДВИЖОК  (все деньги считает КОД, не ИИ — точно и предсказуемо)
# ──────────────────────────────────────────────────────────────────────────

HOLD_ESTIMATE = {                       # горизонт -> понятный срок удержания
    "intraday": "часы, в пределах дня",
    "swing": "дни–недели (ориентир 3–15 дней)",
    "position": "недели–месяцы",
}


def _base_unit(symbol):                 # "BTC/USDT" -> "BTC"
    return symbol.split("/")[0]


def _entry_mid(entry):                  # берём середину зоны входа
    if isinstance(entry, dict):
        if entry.get("range"):
            r = entry["range"]; return (float(r[0]) + float(r[1])) / 2.0
        if entry.get("value") is not None:
            return float(entry["value"])
    return float(entry)


def compute_risk(symbol, tech, current_heat_pct, md=None):
    """Точный расчёт: размер, R_net, гейты. Это ядро защиты капитала —
    поэтому его считает код, а не языковая модель."""
    m = MANDATE; cap = m["capital"]; md = md or {}
    try:
        entry = _entry_mid(tech.get("entry"))
        stop = float(tech["stop"]["value"])
        targets = [float(t["value"]) for t in tech.get("targets", []) if t.get("value") is not None]
    except Exception as e:              # noqa: BLE001
        return {"passed": False, "reasons": [f"не удалось прочитать уровни: {e}"]}

    direction = tech.get("proposed_direction")
    if direction not in ("long", "short"):
        return {"passed": False, "reasons": [f"не указано направление сделки ({direction})"]}
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return {"passed": False, "reasons": ["стоп совпадает со входом"]}
    if not targets:
        return {"passed": False, "reasons": ["нет цели (TP) — R не посчитать"]}

    # Стороны уровней: у лонга стоп НИЖЕ входа, цели ВЫШЕ; у шорта наоборот.
    # Без этой проверки перевёрнутый стоп проходил гейты, а при закрытии
    # записывался как прибыль — статистика врала в нашу пользу.
    if direction == "long":
        stop_ok = stop < entry
        bad_targets = [t for t in targets if t <= entry]
    else:
        stop_ok = stop > entry
        bad_targets = [t for t in targets if t >= entry]
    if not stop_ok:
        side = "ниже" if direction == "long" else "выше"
        return {"passed": False, "reasons": [
            f"стоп {stop} не с той стороны: у {'лонга' if direction == 'long' else 'шорта'} "
            f"он должен быть {side} входа {round(entry, 2)}"]}
    if bad_targets:
        return {"passed": False, "reasons": [
            f"цели {bad_targets} не с той стороны от входа {round(entry, 2)}"]}

    # полный размер под целевой риск (напр. 1%), затем поджимаем потолками
    target_risk_pct = m["risk_per_trade_pct"]
    full_risk_amount = cap * target_risk_pct / 100.0
    full_size = full_risk_amount / risk_per_unit
    size = full_size
    exposure = size * entry
    binding = "none"
    max_expo_conc = cap * m["max_concentration_pct"] / 100.0
    if exposure > max_expo_conc:
        size = max_expo_conc / entry; exposure = size * entry; binding = "concentration"
    max_expo_lev = cap * m["max_leverage"]
    if exposure > max_expo_lev:
        size = max_expo_lev / entry; exposure = size * entry; binding = "leverage"
    risk_amount = size * risk_per_unit
    risk_pct = risk_amount / cap * 100.0

    size_trimmed = binding != "none"
    size_note = ""
    if size_trimmed:
        cap_name = "концентрации 15%" if binding == "concentration" else "плеча"
        size_note = (f"размер урезан потолком {cap_name}: полный под {target_risk_pct}% риска "
                     f"был бы {round(full_size, 6)} ({round(full_risk_amount, 2)}$ риска), "
                     f"но потолок ограничил его; итог — риск {round(risk_pct, 2)}% вместо {target_risk_pct}%")

    target = max(targets, key=lambda t: abs(t - entry))     # дальняя цель для R
    reward_per_unit = abs(target - entry)
    R_gross = reward_per_unit / risk_per_unit

    # Издержки платятся ДВАЖДЫ: на входе и на выходе (раньше считали один раз).
    c = COSTS_HINT
    total_costs = 2 * exposure * (c["commission_pct"] + c["spread_pct"] + c["slippage_pct"]) / 100.0
    # Плата за финансирование фьючерса: начисляется 3 раза в сутки, пока позиция открыта.
    # При положительной ставке платят лонги, при отрицательной — шорты. Считаем только
    # расход (получение не засчитываем в плюс — осторожная оценка).
    fr = md.get("funding_rate") or 0.0
    signed = fr if direction == "long" else -fr
    funding_hold_frac = max(0.0, signed) * 3 * m["assumed_hold_days"]
    funding_cost = exposure * funding_hold_frac
    total_costs += funding_cost
    gross_profit = size * reward_per_unit
    R_net = (gross_profit - total_costs) / risk_amount if risk_amount > 0 else 0.0

    # Стоп не уже рыночного шума: если он ближе доли среднего размаха свечи,
    # его выбьет обычным колебанием (урок сделки SOL со стопом $0.19).
    avg_range = md.get("avg_candle_range_14")
    min_stop = (avg_range or 0) * m["min_stop_vs_candle"]

    heat_after = current_heat_pct + risk_pct
    gates = {
        "has_stop": True,
        "size_within_caps": size > 0 and exposure <= max_expo_conc + 1e-6,
        "R_net_ge_min": R_net >= m["min_R"],
        "heat_ok": heat_after <= m["max_heat_pct"] + 1e-9,
        "stop_not_too_tight": risk_per_unit >= min_stop,
    }
    reasons = []
    if not gates["R_net_ge_min"]:
        reasons.append(f"чистая R={round(R_net,2)} ниже порога {m['min_R']}")
    if not gates["heat_ok"]:
        reasons.append("превышен совокупный риск (heat)")
    if not gates["stop_not_too_tight"]:
        reasons.append(f"стоп слишком узкий: {round(risk_per_unit, 4)} от входа при среднем "
                       f"размахе свечи {avg_range} — выбьет рыночным шумом")

    return {
        "instrument": symbol, "asset_class": "crypto", "direction": direction,
        "entry": round(entry, 2), "stop": round(stop, 2),
        "targets": [round(t, 2) for t in targets], "target_far": round(target, 2),
        "size": {"value": round(size, 6), "unit_type": _base_unit(symbol)},
        "risk_amount": round(risk_amount, 2), "risk_pct": round(risk_pct, 3),
        "R_gross": round(R_gross, 2), "costs_total": round(total_costs, 2), "R_net": round(R_net, 2),
        "funding_cost": round(funding_cost, 2), "funding_hold_frac": funding_hold_frac,
        "exposure_pct": round(exposure / cap * 100.0, 2), "leverage_used": round(exposure / cap, 2),
        "heat_after_pct": round(heat_after, 3), "size_binding_cap": binding,
        "risk_target_pct": target_risk_pct, "full_size": round(full_size, 6),
        "size_trimmed": size_trimmed, "size_note": size_note,
        "hold_estimate": HOLD_ESTIMATE.get(tech.get("horizon", m["sprint_horizon"]), ""),
        "gates": gates, "passed": all(gates.values()), "reasons": reasons,
    }


# ──────────────────────────────────────────────────────────────────────────
# 6. ОРКЕСТРАЦИЯ
# ──────────────────────────────────────────────────────────────────────────

def analyze_instrument(symbol, ctx):
    """Тех ‖ Новости (ИИ, параллельно) -> Риск (КОД) -> гейты контекста (КОД)."""
    md = build_market_data(symbol)
    items = N.items_for(symbol, ctx["news_all"])
    lessons = MEM.lessons_for(symbol)
    tech_payload = {"instrument": symbol, "asset_class": "crypto",
                    "sprint_horizon": MANDATE["sprint_horizon"], "sprint_goal": SPRINT_GOAL,
                    "mandate": {"min_R": MANDATE["min_R"]}, "market_data": md,
                    "market_regime": ctx["regime"], "portfolio": ctx["portfolio"], "lessons": lessons}
    news_payload = {"instrument": symbol, "asset_class": "crypto",
                    "sprint_horizon": MANDATE["sprint_horizon"], "news_items": items,
                    "fear_greed": ctx["fear_greed"],
                    "proxies": {"funding_rate": md.get("funding_rate"),
                                "open_interest": md.get("open_interest"),
                                "change_pct_window": md.get("change_pct_window")}}
    # Отсев кодом: если тренд монеты и режим рынка не оставляют ни одного разрешённого
    # направления, гейты всё равно отклонят любой сетап — теханалитика не зовём.
    allowed, skip_reason = C.allowed_directions(md, ctx["regime"])
    if skip_reason:
        tech = {"instrument": symbol, "setup_found": False, "regime": md.get("trend"),
                "notes": f"отсеяно кодом до анализа: {skip_reason}", "skipped_by_code": True}
        try:
            news = call_agent("news", news_payload, f"news::{symbol}")
        except Exception:                 # noqa: BLE001
            news = {"has_catalyst": False, "claims": [], "confidence": "low", "model_failed": True}
    else:
        tech_payload["allowed_directions"] = allowed
        with ThreadPoolExecutor(max_workers=MODEL_PARALLEL) as ex:
            f_tech = ex.submit(call_agent, "tech", tech_payload, f"tech::{symbol}")
            f_news = ex.submit(call_agent, "news", news_payload, f"news::{symbol}")
            try:
                tech = f_tech.result()
            except Exception as e:        # noqa: BLE001 — модель недоступна: монету не теряем
                tech = {"instrument": symbol, "setup_found": False,
                        "notes": f"теханалитик не ответил: модель недоступна ({str(e)[:80]})", "model_failed": True}
            try:
                news = f_news.result()
            except Exception as e:        # noqa: BLE001
                news = {"has_catalyst": False, "claims": [], "confidence": "low", "model_failed": True}

    news = N.validate(news, items)            # утверждения без источника — выброшены
    MARKET_VIEW[symbol] = {                   # новости в отчёт — даже если сделки не будет
        "sentiment": news.get("sentiment"), "has_catalyst": news.get("has_catalyst"),
        "catalyst": news.get("catalyst", ""), "event_risk_flag": news.get("event_risk_flag"),
        "event_risk": news.get("event_risk", "") if news.get("event_risk_flag") else "",
        "claims": news.get("claims", []), "sources": news.get("sources", []),
        "news_found": len(items), "notes": news.get("validation_notes", []),
        "trend_4h": md.get("trend"), "last_price": md.get("last_price")}
    notes = [n for n in [C.check_trend_claim(tech, md)] if n]
    notes += news.get("validation_notes", [])
    positioning = D.positioning(_perp, symbol, md.get("funding_rate"))
    base = {"instrument": symbol, "tech": tech, "news": news, "md": md,
            "lessons": lessons, "notes": notes, "positioning": positioning}

    if not tech.get("setup_found", False):
        return dict(base, risk=None, reason=tech.get("notes") or "нет валидного сетапа")

    risk = compute_risk(symbol, tech, PORTFOLIO_STATE["current_heat_pct"], md)
    if "gates" in risk:                       # уровни прочитаны — проверяем контекст
        g, rs = C.context_gates(symbol, risk["direction"], md, ctx["regime"],
                                PORTFOLIO_STATE["open_positions"])
        risk["gates"].update(g)
        risk["reasons"] = risk.get("reasons", []) + rs
        risk["passed"] = all(risk["gates"].values())
        crowd = D.warning_for(risk["direction"], positioning)
        if crowd:
            notes.append(f"предупреждение: {crowd}")
    log_step(f"risk_calc::{symbol}", "risk_engine(code)", "-", tech, risk)
    return dict(base, risk=risk)


def _closed_trades():
    try:
        with open("results.jsonl", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def drawdown_guard():
    """Защитные стопы мандата — теперь их проверяет КОД перед каждым спринтом.
    Возвращает причину остановки или None, если торговать можно.
    Считаются закрытые сделки (results.jsonl), в процентах от капитала."""
    m = MANDATE; cap = m["capital"]; now = datetime.datetime.now()
    rows = _closed_trades()

    def pct(subset):
        return round(sum(r.get("pnl", 0) or 0 for r in subset) / cap * 100, 2)

    def closed_dt(r):
        try:
            return datetime.datetime.fromisoformat(str(r.get("closed_at")))
        except ValueError:
            return datetime.datetime.min

    today = [r for r in rows if closed_dt(r).date() == now.date()]
    sprint = [r for r in rows if closed_dt(r) >= now - datetime.timedelta(days=m["sprint_days"])]
    checks = [
        ("АВАРИЙНЫЙ СТОП (kill-switch)", pct(rows), m["killswitch_pct"],
         "система остановлена до ручного разбора: нужен пересмотр стратегии"),
        (f"стоп спринта (за {m['sprint_days']} дн.)", pct(sprint), m["sprint_stop_pct"],
         "новые сделки — после того, как окно в несколько дней очистится от убытков"),
        ("дневной стоп", pct(today), m["daily_stop_pct"], "новые сделки — завтра"),
    ]
    for name, value, limit, what_next in checks:
        if value <= limit:
            return f"сработал {name}: результат {value}% при лимите {limit}%; {what_next}"
    return None


def _no_trade(reason):
    package = {"sprint_id": RUN_ID, "goal": SPRINT_GOAL, "candidates": [],
               "no_trade": True, "no_trade_reason": reason,
               "total_heat_pct": PORTFOLIO_STATE["current_heat_pct"]}
    _save_briefing({"sprint_id": RUN_ID, "summary": f"Сделок нет. {reason}",
                    "candidates_plain": [], "terms_used": []}, package, None, None)


def _prepare_context():
    """Один раз за запуск: разбор прошлых сделок, режим рынка, новости, портфель."""
    global _perp
    print("  • контекст: уроки прошлых сделок, режим рынка, новости ...")
    if _perp == "auto":
        _perp = MK.perp()
    print(f"    модели: {', '.join(sorted(set(llm.describe().values())))}")
    print(f"    данные: спот — {getattr(_spot, 'id', '?')}; фьючерсы — {getattr(_perp, 'id', '?') if _perp else 'недоступны'}")
    try:
        new_lessons = MEM.review_closed(_spot)
        MEM.review_rejected(_spot)
        if new_lessons:
            print(f"    новых уроков: {len(new_lessons)}")
    except Exception as e:                # noqa: BLE001
        print(f"    ! разбор прошлых сделок не удался: {e}")
    regime = C.market_regime(_spot)
    print(f"    {regime['note']}")
    news_all, errors = N.fetch_all()
    for e in errors:
        print(f"    ! лента новостей недоступна — {e}")
    fng = N.fear_greed()
    if fng:
        print(f"    индекс страха/жадности: {fng['value']} ({fng['label']})")
    portfolio = C.portfolio_block(PORTFOLIO_STATE, P.load_results())
    MARKET_CTX.update({"regime": regime, "fear_greed": fng})
    # Выбранное направление против режима рынка: сделок почти не будет — предупреждаем сразу.
    conflict = {("long", "down"): "падает", ("short", "up"): "растёт"}.get((CHOSEN_DIRECTION, regime.get("regime")))
    if conflict:
        note = (f"выбрано направление «{CHOSEN_DIRECTION}», а рынок (BTC) {conflict}: код пропустит сделки "
                f"только по режиму рынка, а эксперт снимет их как противоречащие выбору. "
                f"Для такого рынка лучше запускать с направлением any")
        MARKET_CTX["direction_note"] = note
        print(f"    ⚠ {note}")
    return {"regime": regime, "news_all": news_all, "fear_greed": fng, "portfolio": portfolio}


def _snapshot(md):
    keys = ("last_price", "trend", "sma_fast_10", "sma_slow_30", "range_position_pct",
            "swing_high_20", "swing_low_20", "avg_candle_range_14", "change_pct_window", "funding_rate")
    return {k: md.get(k) for k in keys}


def _log_decisions(analyses, regime, cand_ids, report_1=None, report_2=None):
    """Журнал решений для обучения: что предлагал каждый агент и что решил код."""
    cons = {o.get("id"): o.get("verdict") for o in (report_2 or {}).get("clean_options", [])
            if isinstance(o, dict)}
    expert = {v.get("id"): v.get("allowed") for v in (report_1 or {}).get("verdict_per_candidate", [])
              if isinstance(v, dict)}
    for a in analyses:
        t, r = a.get("tech", {}), a.get("risk") or {}
        cid = cand_ids.get(a["instrument"])
        try:
            MEM.log_decision({
                "run_id": RUN_ID, "instrument": a["instrument"],
                "setup_found": bool(t.get("setup_found")),
                "direction": r.get("direction") or t.get("proposed_direction"),
                "passed": bool(r.get("passed")) and cid is not None,
                "reasons": r.get("reasons") or ([a["reason"]] if a.get("reason") else []),
                "entry": r.get("entry"), "stop": r.get("stop"), "target_far": r.get("target_far"),
                "R_net": r.get("R_net"), "trend_4h": a.get("md", {}).get("trend"),
                "regime": (regime or {}).get("regime"), "tech_confidence": t.get("confidence"),
                "news_has_catalyst": a.get("news", {}).get("has_catalyst"),
                "candidate_id": cid,
                "consultant_verdict": cons.get(cid) if cid else None,
                "expert_allowed": expert.get(cid) if cid else None,
            })
        except Exception as e:            # noqa: BLE001
            print(f"    ! журнал решений: {e}")


_LONG_FIXES = [(r"\bв долг\b", "в лонг (ставка на рост)"),
               (r"\bдолгов\w* позици\w*", "лонг (ставка на рост)")]


def _sanitize(obj):
    """Страховка от «в долг» вместо «лонг» в тексте переводчика."""
    if isinstance(obj, str):
        for pat, rep in _LONG_FIXES:
            obj = re.sub(pat, rep, obj, flags=re.IGNORECASE)
        return obj
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    return obj


def run_sprint():
    print(f"\n=== Спринт {RUN_ID} — фаза 1a (бумага) ===\n")

    stop_reason = drawdown_guard()
    if stop_reason:
        print(f"  ⛔ {stop_reason}")
        _no_trade(stop_reason)
        return

    # Монеты с уже открытой позицией не анализируем: второй сделки по ним не будет.
    held = {p.get("instrument") for p in PORTFOLIO_STATE["open_positions"]}
    scan = [s for s in WATCHLIST if s not in held]
    print(f"Сканируем: {', '.join(scan) or '—'}  | лог: {LOG_PATH}")
    if held:
        print(f"Пропускаем (уже есть открытая позиция): {', '.join(sorted(held))}")
    print()

    ctx = _prepare_context()

    analyses = []
    for symbol in scan:
        print(f"  • анализ {symbol} ...")
        try:
            analyses.append(analyze_instrument(symbol, ctx))
        except Exception:                 # noqa: BLE001
            print(f"    ! ошибка на {symbol}:\n{traceback.format_exc()}")

    passing = [a for a in analyses if a.get("risk") and a["risk"].get("passed")]

    # ранжируем по R_net и держим совокупный heat в пределах мандата (код)
    passing.sort(key=lambda a: a["risk"]["R_net"], reverse=True)
    heat = PORTFOLIO_STATE["current_heat_pct"]; kept = []
    for a in passing:
        if heat + a["risk"]["risk_pct"] <= MANDATE["max_heat_pct"] + 1e-9:
            heat += a["risk"]["risk_pct"]; kept.append(a)
        else:
            a["risk"]["passed"] = False
            a["risk"]["reasons"].append("не поместился в лимит риска пакета (heat)")
    passing = kept

    # никто не прошёл — код формирует no_trade с ВНЯТНОЙ причиной
    if not passing:
        parts = []
        for a in analyses:
            r = a.get("risk")
            why = "; ".join(r["reasons"]) if (r and r.get("reasons")) else a.get("reason", "нет сетапа")
            parts.append(f"{a['instrument']}: {why}")
        reason = " | ".join(parts) or "нет подходящих сетапов A-класса"
        print(f"\n  Итог: НЕТ СДЕЛКИ — {reason}\n  (Пустой спринт — нормальный результат.)")
        _log_decisions(analyses, ctx["regime"], {})
        _no_trade(reason)
        return

    cand_ids = {a["instrument"]: f"C-{i+1:04d}" for i, a in enumerate(passing)}

    # стратег — ТОЛЬКО текст (тезис/контр-тезис/ранг), числа не трогает
    print("  • трейдер-стратег: тезисы и ранжирование ...")
    strat_in = {"sprint_goal": SPRINT_GOAL, "market_regime": ctx["regime"],
                "portfolio": ctx["portfolio"], "candidates": [
        {"id": cand_ids[a["instrument"]], "instrument": a["instrument"], "direction": a["risk"]["direction"],
         "tech_notes": a["tech"].get("notes", ""), "catalyst": a["news"].get("catalyst", ""),
         "has_catalyst": a["news"].get("has_catalyst", False), "R_net": a["risk"]["R_net"],
         "lessons": a["lessons"]}
        for a in passing]}
    try:
        narrative = call_agent("strategist", strat_in, "strategist")
        narr = {n.get("id"): n for n in narrative.get("candidates", [])}
    except Exception:                     # noqa: BLE001
        narr = {}

    # КОД собирает пакет с верными числами (нарратив — из стратега)
    candidates = []
    for i, a in enumerate(passing):
        cid = cand_ids[a["instrument"]]; r = a["risk"]; n = narr.get(cid, {})
        candidates.append({
            "id": cid, "decision_id": f"{RUN_ID}:{a['instrument']}",
            "instrument": a["instrument"], "asset_class": "crypto",
            "direction": r["direction"], "horizon": a["tech"].get("horizon", MANDATE["sprint_horizon"]),
            "thesis": n.get("thesis") or a["tech"].get("notes", ""),
            "counter_thesis": n.get("counter_thesis") or "риск: движение против позиции; катализатора нет",
            "catalyst": a["news"].get("catalyst", ""), "has_catalyst": a["news"].get("has_catalyst", False),
            "news_sources": a["news"].get("sources", []), "notes": a.get("notes", []),
            "entry": r["entry"], "stop": r["stop"], "targets": r["targets"], "target_far": r["target_far"],
            "size": r["size"], "risk_amount": r["risk_amount"], "risk_pct": r["risk_pct"],
            "R_gross": r["R_gross"], "expected_R_net": r["R_net"], "costs_total": r["costs_total"],
            "funding_cost": r["funding_cost"], "funding_hold_frac": r["funding_hold_frac"],
            "exposure_pct": r["exposure_pct"], "leverage_used": r["leverage_used"],
            "size_binding_cap": r["size_binding_cap"], "size_note": r.get("size_note", ""),
            "hold_estimate": r["hold_estimate"],
            "dod_gates": r["gates"], "rank": i + 1,
        })
    package = {"sprint_id": RUN_ID, "goal": SPRINT_GOAL, "capital": MANDATE["capital"],
               "market_regime": ctx["regime"], "fear_greed": ctx["fear_greed"],
               "candidates": candidates, "no_trade": False, "no_trade_reason": None,
               "total_heat_pct": round(heat, 3)}

    lessons_by = {a["instrument"]: a["lessons"] for a in passing}
    snapshot = {a["instrument"]: _snapshot(a["md"]) for a in passing}
    by_sym = {a["instrument"]: a for a in passing}

    # дебаты: адвокат лонга ↔ адвокат шорта, консультант №2 — судья.
    # У всех одни и те же факты: рынок, режим, новости с источниками, позиционирование, уроки.
    print("  • дебаты лонг ↔ шорт, консультант №2 — судья ...")
    options = []
    for c in candidates:
        a = by_sym[c["instrument"]]
        facts = DB.facts_for(c, snapshot[c["instrument"]], ctx["regime"], c.get("news_sources", []),
                             a.get("positioning"), lessons_by[c["instrument"]])
        options.append(DB.run(c, facts, call_agent, parallel=MODEL_PARALLEL))
    report_2 = {"sprint_id": RUN_ID, "clean_options": options,
                "filtered_out": [o["id"] for o in options if o["verdict"] == "veto"]}

    for o in options:                     # дебаты не проведены из-за лимита — помечаем идею, но не снимаем
        if o["verdict"] == "unchecked":
            for c in candidates:
                if c["id"] == o["id"]:
                    c.setdefault("notes", []).append("⚠ дебаты и проверка судьёй не проведены — лимит модели; "
                                                     "решайте осторожнее")
    vetoed = {o["id"]: o["veto_reason"] for o in options if o["verdict"] == "veto"}
    if MANDATE["honor_vetoes"] and vetoed:
        for c in candidates:
            if c["id"] in vetoed:
                r = by_sym[c["instrument"]]["risk"]
                r["passed"] = False
                r["reasons"].append(f"вето консультанта: {vetoed[c['id']]}")
        package["rejected_candidates"] = [dict(c, rejected_by="консультант №2", reason=vetoed[c["id"]])
                                          for c in candidates if c["id"] in vetoed]
        candidates = [c for c in candidates if c["id"] not in vetoed]
        package["candidates"] = candidates

    # Эксперт №1 — независимо от судьи (не видит отчёт №2), про капитал и портфель.
    report_1 = None
    if candidates:
        print("  • Эксперт №1: капитал и портфель ...")
        try:
            report_1 = call_agent("capital_manager",
                                  {"package": package, "mandate": MANDATE,
                                   "portfolio_state": ctx["portfolio"], "lessons": lessons_by}, "report_1")
        except Exception as e:            # noqa: BLE001
            report_1 = {"verdict_per_candidate": [], "expert_opinion": f"эксперт не ответил: {e}"}
        refused = {v.get("id"): "; ".join(v.get("risks") or []) or v.get("notes", "")
                   for v in report_1.get("verdict_per_candidate", [])
                   if isinstance(v, dict) and v.get("allowed") is False}
        if MANDATE["honor_vetoes"] and refused:
            for c in candidates:
                if c["id"] in refused:
                    r = by_sym[c["instrument"]]["risk"]
                    r["passed"] = False
                    r["reasons"].append(f"эксперт №1 против: {refused[c['id']] or 'без пояснения'}")
            package.setdefault("rejected_candidates", []).extend(
                dict(c, rejected_by="эксперт №1", reason=refused[c["id"]]) for c in candidates if c["id"] in refused)
            candidates = [c for c in candidates if c["id"] not in refused]
            package["candidates"] = candidates

    _log_decisions(analyses, ctx["regime"], cand_ids, report_1, report_2)

    if not candidates:
        reasons = [f"{x['instrument']}: {x['rejected_by']} — {x['reason']}"
                   for x in package.get("rejected_candidates", [])]
        reason = "все кандидаты сняты проверкой: " + " | ".join(reasons)
        print(f"\n  Итог: НЕТ СДЕЛКИ — {reason}")
        package.update({"no_trade": True, "no_trade_reason": reason})
        _save_briefing({"sprint_id": RUN_ID, "summary": f"Сделок нет. {reason}",
                        "candidates_plain": [], "terms_used": []}, package, report_1, report_2)
        return
    package["total_heat_pct"] = round(PORTFOLIO_STATE["current_heat_pct"] +
                                      sum(c["risk_pct"] for c in candidates), 3)

    # переводчик -> понятный текст (числа берёт из package). Ему — сжатая выжимка,
    # а не все данные: у бесплатной модели лимит на размер запроса. Если переводчик
    # всё равно не ответил, отчёт собирает код — идеи не должны теряться.
    print("  • переводчик готовит понятный отчёт ...\n")
    try:
        briefing = _sanitize(call_agent("translator", _translator_payload(package, report_1, report_2),
                                        "translator"))
    except Exception as e:                # noqa: BLE001
        print(f"    ! переводчик не ответил ({str(e)[:120]}) — отчёт собран кодом")
        briefing = _fallback_briefing(package, report_1, report_2)
    _save_briefing(briefing, package, report_1, report_2)
    _print_briefing(briefing, package)


def _translator_payload(package, report_1, report_2):
    """Компактная выжимка для переводчика: только то, что он пересказывает."""
    keep = ("id", "instrument", "direction", "thesis", "counter_thesis", "catalyst", "entry", "stop",
            "targets", "target_far", "risk_amount", "risk_pct", "expected_R_net", "hold_estimate", "size_note")
    cands = [{k: c.get(k) for k in keep} for c in package.get("candidates", [])]
    expert = [v for v in (report_1 or {}).get("verdict_per_candidate", []) if isinstance(v, dict)]
    judge = [{"id": o.get("id"), "winner": o.get("winner"), "verdict": o.get("verdict"), "reason": o.get("reason"),
              "long_strongest": (o.get("long_case") or {}).get("strongest"),
              "short_strongest": (o.get("short_case") or {}).get("strongest")}
             for o in (report_2 or {}).get("clean_options", [])]
    return {"package": {"sprint_id": package.get("sprint_id"), "candidates": cands,
                        "market_regime": (package.get("market_regime") or {}).get("note")},
            "report_1": {"verdict_per_candidate": expert,
                         "expert_opinion": (report_1 or {}).get("expert_opinion", "")},
            "report_2": {"clean_options": judge}, "glossary": GLOSSARY}


def _fallback_briefing(package, report_1, report_2):
    """Отчёт без переводчика: те же поля, текст из данных команды и экспертов."""
    expert = {v.get("id"): v for v in (report_1 or {}).get("verdict_per_candidate", []) if isinstance(v, dict)}
    judge = {o.get("id"): o for o in (report_2 or {}).get("clean_options", [])}
    plain = []
    for c in package.get("candidates", []):
        side = "Купить (лонг — ставка на рост)" if c["direction"] == "long" else "Продать в шорт (ставка на падение)"
        e, j = expert.get(c["id"], {}), judge.get(c["id"], {})
        plain.append({"id": c["id"], "what": f"{side} {c['instrument']}", "why": c.get("thesis", ""),
                      "what_could_go_wrong": c.get("counter_thesis", ""),
                      "expert_said": e.get("notes") or "; ".join(e.get("risks") or []),
                      "consultant_said": j.get("reason", ""),
                      "agreement": "оба за" if e.get("allowed") is not False and j.get("verdict") == "pass" else "есть расхождение"})
    n = len(plain)
    return {"sprint_id": package.get("sprint_id"), "summary": f"Идей для входа: {n}." if n else "Сделок нет.",
            "candidates_plain": plain, "terms_used": [], "assembled_by_code": True}


# ──────────────────────────────────────────────────────────────────────────
# 6. ВЫВОД И СОХРАНЕНИЕ
# ──────────────────────────────────────────────────────────────────────────

MARKET_VIEW = {}       # монета -> разбор новостей и настроения (заполняется за прогон)
MARKET_CTX = {}        # режим рынка и индекс страха/жадности


def _unload_models():
    """После прогона выгружаем локальную модель из памяти, иначе Ollama держит её
    ещё несколько минут и Mac остаётся тяжёлым."""
    if llm.IN_CLOUD or "localhost:11434" not in PROVIDER_BASE_URL:
        return
    import urllib.request
    base = PROVIDER_BASE_URL.split("/v1")[0]
    for m in set(MODEL_BY_AGENT.values()) | {DEFAULT_MODEL}:
        try:
            req = urllib.request.Request(base + "/api/generate", method="POST",
                                         data=json.dumps({"model": m, "keep_alive": 0}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:                 # noqa: BLE001
            pass


def _save_briefing(briefing, package, r1, r2):
    path = f"briefings/briefing_{RUN_ID}.json"
    package["llm_usage"] = llm.run_summary(RUN_ID)
    if llm.FAILURES:
        package["model_limit_note"] = (f"лимит бесплатной модели исчерпан: не ответили {len(llm.FAILURES)} "
                                       f"вызов(ов) — {', '.join(sorted(set(llm.FAILURES)))}. Часть проверок "
                                       f"не выполнена; лимит обновится в течение суток")
    package.setdefault("market_news", MARKET_VIEW)
    if MARKET_CTX:
        package.setdefault("direction", CHOSEN_DIRECTION)
        if MARKET_CTX.get("direction_note"):
            package.setdefault("direction_note", MARKET_CTX["direction_note"])
        package.setdefault("market_regime", MARKET_CTX.get("regime"))
        package.setdefault("fear_greed", MARKET_CTX.get("fear_greed"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"briefing": briefing, "package": package,
                   "report_1": r1, "report_2": r2}, f, ensure_ascii=False, indent=2)
    print(f"  Сохранено: {path}")


SENT_RU = {"positive": "позитивное", "negative": "негативное", "neutral": "нейтральное", "mixed": "смешанное"}


def _print_market_news():
    if not MARKET_VIEW:
        return
    print("\n" + "═" * 60)
    print("НОВОСТИ И НАСТРОЕНИЕ ПО МОНЕТАМ")
    print("═" * 60)
    for sym, v in MARKET_VIEW.items():
        print(f"▸ {sym}: настроение {SENT_RU.get(v.get('sentiment'), v.get('sentiment') or '—')}, "
              f"новостей за 48 ч: {v.get('news_found', 0)}")
        if v.get("has_catalyst"):
            print(f"    Катализатор: {v['catalyst']}")
        if v.get("event_risk"):
            print(f"    ⚠ Риск события: {v['event_risk']}")
        for c in v.get("claims", [])[:3]:
            print(f"    – {c['text']} {''.join(f'[{r}]' for r in c['refs'])}")
        for s in v.get("sources", []):
            print(f"      [{s['n']}] {s['source']}: {s['title']}  {s['link']}")


def _print_briefing(b, package=None):
    cards = {c["id"]: c for c in (package or {}).get("candidates", [])}
    print("\n" + "═" * 60)
    print("ИТОГ СПРИНТА — простыми словами")
    print("═" * 60)
    reg = (package or {}).get("market_regime") or {}
    if reg.get("note"):
        print(f"Рынок: {reg['note']}")
    print(b.get("summary", ""), "\n")
    for c in b.get("candidates_plain", []):
        card = cards.get(c.get("id"), {})
        print(f"▸ {c.get('what','')}")
        print(f"  Почему: {c.get('why','')}")
        print(f"  Риск:   {c.get('what_could_go_wrong','')}")
        if card:                                    # карта действий — точные числа из кода
            side = "КУПИТЬ (лонг)" if card.get("direction") == "long" else "ПРОДАТЬ в шорт"
            tps = " / ".join(str(t) for t in card.get("targets", []))
            print("  ── что делать ──")
            print(f"    Направление: {side}")
            print(f"    Вход:        ~{card.get('entry')}")
            print(f"    Стоп:        {card.get('stop')}  (выход с убытком)")
            print(f"    Цели:        {tps}  (фиксация прибыли)")
            print(f"    Размер:      {card.get('size',{}).get('value')} {card.get('size',{}).get('unit_type','')}"
                  f"  (риск ~${card.get('risk_amount')} = {card.get('risk_pct')}% капитала)")
            if card.get("size_note"):
                print(f"                 ⓘ {card.get('size_note')}")
            print(f"    Срок:        {card.get('hold_estimate','')}")
            print(f"    Когда продавать: по достижении цели (прибыль) ИЛИ по стопу (убыток);")
            print(f"                     если за срок ничего не произошло — пересмотреть.")
        # объяснение чисел печатаем ИЗ КОДА (точные), не из текста переводчика
        risk_amt = card.get("risk_amount"); Rn = card.get("expected_R_net")
        if risk_amt is not None and Rn is not None:
            goal_profit = round(risk_amt * Rn, 2)
            print(f"    – Риск ~${risk_amt}: если цену выбьет по стопу, потеряете примерно эту сумму.")
            print(f"    – R_net {Rn}: на каждый $1 риска ждём ~${Rn} прибыли после комиссий; "
                  f"риск здесь ~${risk_amt}, цель по прибыли ~${goal_profit}.")
        else:
            for n in c.get("numbers_explained", []):
                print(f"    – {n.get('label','')}: {n.get('means','')}")
        for src in card.get("news_sources", []):
            print(f"    [{src['n']}] {src['source']}: {src['title']}  {src['link']}")
        for note in card.get("notes", []):
            print(f"    ⓘ {note}")
        print(f"  Эксперт №1: {c.get('expert_said','')}")
        print(f"  Консультант №2: {c.get('consultant_said','')}")
        print(f"  → {c.get('agreement','')}\n")
    for x in (package or {}).get("rejected_candidates", []):
        print(f"✗ Снято: {x['instrument']} — {x['rejected_by']}: {x['reason']}")
    if b.get("terms_used"):
        print("Новые слова:", ", ".join(t.get("term", "") for t in b["terms_used"]))
    print("\nРешение за вами. (Фаза 1a: это бумага, сделок не исполняем.)")


if __name__ == "__main__":
    # Ollama работает локально и ключа не требует. Убедитесь, что Ollama запущена
    # (приложение открыто) и модель скачана: ollama pull qwen2.5:7b
    import sys as _sys
    _args = _sys.argv[1:]
    if "--direction" in _args and len(_args) > _args.index("--direction") + 1:
        _dir = _args[_args.index("--direction") + 1]
        _dir = _dir if _dir in GOAL_BY_DIRECTION else "any"
    else:
        _dir = ask_direction()
    SPRINT_GOAL = GOAL_BY_DIRECTION[_dir]
    CHOSEN_DIRECTION = _dir
    print(f"\nНаправление: {_dir.upper()} — начинаю анализ...")
    try:
        run_sprint()
        _print_market_news()
    finally:
        _unload_models()


# ──────────────────────────────────────────────────────────────────────────
# КАК СМЕНИТЬ МОДЕЛИ: llm.py, словарь ROLE_CHAINS — для каждой роли цепочка
# «провайдер, модель». Ключи провайдеров — в переменных окружения
# (GROQ_API_KEY, OLLAMA_API_KEY); в GitHub — в Settings → Secrets → Actions.
# ──────────────────────────────────────────────────────────────────────────
