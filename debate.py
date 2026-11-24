# -*- coding: utf-8 -*-
"""
ДЕБАТЫ: команда «лонг» против команды «шорт», консультант — судья.

Так в одной системе живут три задуманные команды без тройных затрат:
факты собирает общая команда, а мнения делятся:
  - адвокат лонга  — лучшие доводы ЗА рост
  - адвокат шорта  — лучшие доводы ЗА падение
  - консультант №2 — независимый судья: читает обе стороны и выбирает сильнейшую

Правила кода (модель их не обойдёт):
  - довод без цифры или факта из данных помечается как «риторика» и весит меньше;
  - если судья выбрал сторону, противоположную сделке, или никого — это вето;
  - вето снимает сделку из отчёта (настройка honor_vetoes в мандате pipeline.py).
Каждое решение судьи пишется в журнал, а табель потом покажет, насколько он точен.
"""

import re
from concurrent.futures import ThreadPoolExecutor

DIR_RU = {"long": "лонг", "short": "шорт"}
_HAS_EVIDENCE = re.compile(r"\d|\[\d+\]")      # число или ссылка на новость [N]


_AGAINST = {"long": re.compile(r"тренд\w*\s+(вниз|нисходящ)|нисходящ\w*\s+тренд|ниже\s+(средн|sma)", re.I),
            "short": re.compile(r"тренд\w*\s+(вверх|восходящ)|восходящ\w*\s+тренд|выше\s+(средн|sma)", re.I)}
_FUNDING_ZERO = re.compile(r"(funding|фондирован|финансирован)\w*[^.]{0,40}0[.,]0+\s*%?", re.I)


def _flag_rhetoric(arg_out, side=None, funding=None):
    """Помечает доводы, которые нельзя засчитать:
    - без цифр и ссылок (риторика);
    - свидетельствующие против своей же стороны (тренд вниз как довод за рост);
    - нулевой funding как довод (это отсутствие перекоса, а не сигнал).
    Возвращает (доводы, сколько не засчитано)."""
    args = []
    for a in (arg_out or {}).get("arguments") or []:
        if not isinstance(a, dict):
            a = {"text": str(a)}
        text = f"{a.get('text', '')} {a.get('evidence', '')}"
        flags = []
        if not _HAS_EVIDENCE.search(text):
            flags.append("нет цифр")
        if side in _AGAINST and _AGAINST[side].search(a.get("text", "")):
            flags.append("довод против своей стороны")
        if funding is not None and abs(funding) < 0.0003 and _FUNDING_ZERO.search(a.get("text", "")):
            flags.append("нулевой funding — не сигнал")
        args.append(dict(a, rhetoric=bool(flags), flags=flags))
    return args, sum(1 for a in args if a["rhetoric"])


def _funding_meaning(fr):
    """Толкование ставки финансирования — кодом, чтобы модель не путала знак."""
    if fr is None:
        return "нет данных"
    pct = round(fr * 100, 4)
    if fr >= 0.0005:
        return f"{pct}% за 8 ч — высокая: лонгов больше, они перегреты (риск отката вниз)"
    if fr <= -0.0003:
        return f"{pct}% за 8 ч — отрицательная: шортов больше, они перегреты (риск резкого роста)"
    return f"{pct}% за 8 ч — около нуля: перекоса нет, это НЕ довод ни за рост, ни за падение"


def _ls_meaning(ls):
    if ls is None:
        return "нет данных"
    if ls >= 2.0:
        return f"{ls} — счетов в лонге в {ls} раза больше: толпа в лонгах"
    if ls <= 0.7:
        return f"{ls} — счетов в шорте больше: толпа в шортах"
    return f"{ls} — заметного перекоса нет"


def facts_for(cand, snapshot, regime, news_sources, positioning, lessons):
    """Один и тот же набор фактов для обеих сторон и судьи.
    Значения, которые маленькая модель путает (funding, перекос лонг/шорт, тренд),
    код сразу снабжает толкованием."""
    snapshot = dict(snapshot or {})
    trend = snapshot.get("trend")
    snapshot["trend_meaning"] = {"up": "восходящий: SMA10 выше SMA30, цена выше SMA30",
                                 "down": "нисходящий: SMA10 ниже SMA30, цена ниже SMA30",
                                 "range": "боковой: явного направления нет"}.get(trend, "нет данных")
    pos = dict(positioning or {})
    pos["funding_meaning"] = _funding_meaning(pos.get("funding_rate"))
    pos["long_short_meaning"] = _ls_meaning(pos.get("long_short_ratio"))
    return {"instrument": cand["instrument"], "proposed_direction": cand["direction"],
            "entry": cand["entry"], "stop": cand["stop"], "target": cand.get("target_far"),
            "expected_R_net": cand.get("expected_R_net"),
            "team_thesis": cand.get("thesis"), "team_counter_thesis": cand.get("counter_thesis"),
            "market": snapshot, "market_regime": regime, "news": news_sources,
            "positioning": pos, "lessons": lessons,
            "trade_mode": cand.get("mode", "trend")}


def run(cand, facts, call_agent, parallel=1):
    """Дебаты по одной сделке. call_agent(key, payload, step) — вызов модели из pipeline.
    Возвращает запись для отчёта №2 в формате clean_options."""
    cid, sym = cand["id"], cand["instrument"]
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        f_long = ex.submit(call_agent, "advocate_long", {"side": "long", "facts": facts}, f"debate_long::{sym}")
        f_short = ex.submit(call_agent, "advocate_short", {"side": "short", "facts": facts}, f"debate_short::{sym}")
        try:
            long_out = f_long.result()
        except Exception as e:                       # noqa: BLE001
            long_out = {"arguments": [], "error": str(e)}
        try:
            short_out = f_short.result()
        except Exception as e:                       # noqa: BLE001
            short_out = {"arguments": [], "error": str(e)}

    if long_out.get("error") and short_out.get("error"):
        # модель недоступна — это НЕ вето: проверку просто не удалось провести
        return {"id": cid, "instrument": sym, "verdict": "unchecked", "winner": "none",
                "veto_reason": "", "reason": "дебаты не проведены: модель недоступна (лимит)",
                "forecast": {"direction": None, "confidence": "low"}, "counter_thesis": "",
                "weak_points": [], "fact_check": "", "long_case": {}, "short_case": {}}

    fr = (facts.get("positioning") or {}).get("funding_rate")
    long_args, long_rh = _flag_rhetoric(long_out, "long", fr)
    short_args, short_rh = _flag_rhetoric(short_out, "short", fr)
    # если у победителя не осталось ни одного засчитанного довода — код не даст ему победить
    valid = {"long": len(long_args) - long_rh, "short": len(short_args) - short_rh}
    judge_in = {"id": cid, "facts": facts,
                "long_case": {"arguments": long_args, "strongest": long_out.get("strongest", ""),
                              "without_evidence": long_rh},
                "short_case": {"arguments": short_args, "strongest": short_out.get("strongest", ""),
                               "without_evidence": short_rh}}
    try:
        j = call_agent("consultant", judge_in, f"judge::{sym}")
    except Exception as e:                           # noqa: BLE001
        return {"id": cid, "instrument": sym, "verdict": "unchecked", "winner": "none", "veto_reason": "",
                "reason": "судья не ответил: модель недоступна (лимит)",
                "forecast": {"direction": None, "confidence": "low"}, "counter_thesis": "", "weak_points": [],
                "fact_check": "", "long_case": {"arguments": long_args}, "short_case": {"arguments": short_args}}

    winner = j.get("winner") if j.get("winner") in ("long", "short") else "none"
    if winner != "none" and valid[winner] == 0:
        winner = "none"
    direction = cand["direction"]
    agreed = winner == direction and str(j.get("verdict", "pass")).lower() != "veto"
    if winner == "none":
        why = "ни у одной стороны не осталось доводов, которые можно засчитать"
    elif winner != direction:
        why = f"судья признал сильнее сторону «{DIR_RU[winner]}», а сделка — «{DIR_RU[direction]}»"
    elif not agreed:
        why = "судья наложил вето"
    else:
        why = ""
    return {
        "id": cid, "instrument": sym, "verdict": "pass" if agreed else "veto",
        "winner": winner, "veto_reason": why,
        "forecast": {"direction": winner if winner != "none" else None,
                     "confidence": j.get("confidence", "low")},
        "reason": j.get("reason", ""), "counter_thesis": j.get("counter_thesis", ""),
        "weak_points": j.get("weak_points", []), "fact_check": j.get("fact_check", ""),
        "long_case": {"strongest": long_out.get("strongest", ""), "arguments": long_args,
                      "without_evidence": long_rh},
        "short_case": {"strongest": short_out.get("strongest", ""), "arguments": short_args,
                       "without_evidence": short_rh},
    }
