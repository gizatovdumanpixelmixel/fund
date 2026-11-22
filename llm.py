# -*- coding: utf-8 -*-
"""
СЛОЙ МОДЕЛЕЙ: какая модель отвечает за какую роль и что делать, если она недоступна.

Все провайдеры говорят на одном языке (OpenAI-совместимый API), поэтому смена
провайдера — это смена строчки в ROLE_CHAINS, а не переделка кода.

  Провайдеры (используются только те, для которых есть ключ):
    groq          — облако Groq, ключ GROQ_API_KEY            (основной)
    ollama_cloud  — облако Ollama, ключ OLLAMA_API_KEY         (запасной)
    ollama_local  — Ollama на вашем Mac, без ключа             (только не в облаке)

  Для каждой роли — цепочка «модель → запасная → ещё запасная». Если модель
  упёрлась в лимит (ошибка 429) или недоступна, берём следующую в цепочке.
  У Groq лимиты считаются отдельно для каждой модели, поэтому сильные роли и
  лёгкие роли сидят на разных моделях — так бесплатный объём фактически больше.

  Учёт: каждый вызов пишется в usage.jsonl — токены и оценка стоимости по платным
  тарифам. Если включён платный режим (LLM_PAID=1) и задан месячный бюджет
  (LLM_MONTHLY_BUDGET_USD), при его исчерпании код переходит на бесплатные варианты.
"""

import os, json, time, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
USAGE_PATH = os.path.join(BASE, "usage.jsonl")
IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "paid_capable": True},
    "ollama_cloud": {"base_url": "https://ollama.com/v1", "key_env": "OLLAMA_API_KEY", "paid_capable": True},
    "ollama_local": {"base_url": os.environ.get("OLLAMA_LOCAL_URL", "http://localhost:11434/v1"),
                     "key": "ollama", "paid_capable": False},
}

STRONG = [("groq", "openai/gpt-oss-120b"), ("ollama_cloud", "gpt-oss:120b"), ("ollama_local", "qwen2.5:7b")]
LIGHT = [("groq", "openai/gpt-oss-20b"), ("groq", "openai/gpt-oss-120b"),
         ("ollama_cloud", "gpt-oss:20b"), ("ollama_local", "qwen2.5:7b")]

# Какие роли требуют рассуждений (сильная модель), а какие — пересказа (лёгкая).
ROLE_CHAINS = {
    "tech": STRONG, "consultant": STRONG, "advocate_long": STRONG, "advocate_short": STRONG,
    "strategist": STRONG, "capital_manager": STRONG,
    "news": LIGHT, "translator": LIGHT,
}

# Насколько долго модель gpt-oss «думает» перед ответом. Размышления тоже стоят токенов.
REASONING = {"tech": "medium", "consultant": "medium", "advocate_long": "low", "advocate_short": "low",
             "strategist": "low", "capital_manager": "low", "news": "low", "translator": "low"}

# Оценка стоимости, $ за 1 млн токенов (вход, выход) — по платным тарифам Groq.
PRICES = {"openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30)}

MAX_WAIT_S = 60          # если провайдер просит подождать дольше — берём следующую модель
MAX_TOKENS = 4000        # с запасом на размышления модели, чтобы JSON не обрезался

_clients = {}
_exhausted = set()       # (провайдер, модель), упёршиеся в дневной лимит за этот запуск
RUN_ID = os.environ.get("RUN_ID", "")


class AllModelsFailed(RuntimeError):
    pass


def _key(provider):
    p = PROVIDERS[provider]
    return p.get("key") or os.environ.get(p["key_env"])


def available(provider):
    if provider == "ollama_local" and IN_CLOUD:
        return False
    return bool(_key(provider))


def _client(provider):
    if provider not in _clients:
        from openai import OpenAI
        _clients[provider] = OpenAI(base_url=PROVIDERS[provider]["base_url"], api_key=_key(provider),
                                    max_retries=0, timeout=180)
    return _clients[provider]


def chain_for(role):
    return [(p, m) for p, m in ROLE_CHAINS.get(role, STRONG) if available(p) and (p, m) not in _exhausted]


# ── учёт и бюджет ────────────────────────────────────────────────────────────

def _cost(model, tin, tout):
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return round((tin * pin + tout * pout) / 1e6, 6)


def _record(role, provider, model, usage, seconds):
    tin = getattr(usage, "prompt_tokens", 0) or 0
    tout = getattr(usage, "completion_tokens", 0) or 0
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "run_id": RUN_ID, "role": role,
           "provider": provider, "model": model, "tokens_in": tin, "tokens_out": tout,
           "cost_usd_if_paid": _cost(model, tin, tout), "seconds": round(seconds, 1)}
    with open(USAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _load_usage():
    if not os.path.exists(USAGE_PATH):
        return []
    with open(USAGE_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def run_summary(run_id=None):
    """Сводка по одному прогону: вызовы, токены, сколько стоило бы на платном тарифе."""
    rid = run_id or RUN_ID
    rows = [r for r in _load_usage() if r.get("run_id") == rid]
    return {"calls": len(rows), "tokens_in": sum(r["tokens_in"] for r in rows),
            "tokens_out": sum(r["tokens_out"] for r in rows),
            "cost_usd_if_paid": round(sum(r["cost_usd_if_paid"] for r in rows), 4),
            "models": sorted({f"{r['provider']}:{r['model']}" for r in rows})}


def month_spend():
    month = datetime.date.today().strftime("%Y-%m")
    return round(sum(r["cost_usd_if_paid"] for r in _load_usage() if r["ts"].startswith(month)), 4)


def budget_left():
    """None — бюджет не ограничен/платный режим выключен; иначе остаток в $."""
    if os.environ.get("LLM_PAID") != "1":
        return None
    cap = float(os.environ.get("LLM_MONTHLY_BUDGET_USD", "0") or 0)
    return round(cap - month_spend(), 4) if cap > 0 else None


# ── вызов ────────────────────────────────────────────────────────────────────

def _retry_after(err):
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for h in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = headers.get(h)
        if not v:
            continue
        v = str(v).strip()
        try:
            if v.endswith("ms"):
                return float(v[:-2]) / 1000
            if v.endswith("s") and "m" in v:              # формат вида 1m30s
                mins, secs = v[:-1].split("m")
                return float(mins) * 60 + float(secs or 0)
            if v.endswith("s"):
                return float(v[:-1])
            return float(v)
        except ValueError:
            continue
    return None


def complete(role, messages, max_tokens=MAX_TOKENS):
    """Отправляет сообщения модели для роли. Возвращает (текст, 'провайдер:модель').
    Переходит по цепочке моделей при лимитах и ошибках."""
    left = budget_left()
    errors = []
    for provider, model in chain_for(role):
        if left is not None and left <= 0 and PROVIDERS[provider]["paid_capable"] and provider != "ollama_local":
            errors.append(f"{provider}:{model}: месячный бюджет исчерпан")
            continue
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=0.3,
                      response_format={"type": "json_object"})
        if provider == "groq" and "gpt-oss" in model:
            kwargs["extra_body"] = {"reasoning_effort": REASONING.get(role, "low")}
        for attempt in range(3):
            t0 = time.time()
            try:
                resp = _client(provider).chat.completions.create(**kwargs)
                _record(role, provider, model, resp.usage, time.time() - t0)
                text = resp.choices[0].message.content or ""
                if not text.strip():
                    raise ValueError("пустой ответ")
                return text, f"{provider}:{model}"
            except Exception as e:                    # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status == 429:
                    wait = _retry_after(e)
                    if wait is not None and wait <= MAX_WAIT_S and attempt < 2:
                        time.sleep(wait + 1)
                        continue
                    _exhausted.add((provider, model))  # дневной лимит — до конца запуска не трогаем
                    errors.append(f"{provider}:{model}: лимит (429)")
                    break
                if status in (400, 404, 401, 403, 413):  # не найдена / нет доступа / запрос велик — следующая
                    errors.append(f"{provider}:{model}: {status} {str(e)[:120]}")
                    break
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                errors.append(f"{provider}:{model}: {str(e)[:160]}")
    raise AllModelsFailed("ни одна модель не ответила: " + " | ".join(errors or ["нет доступных провайдеров — проверьте ключи"]))


def uses_local():
    return any(p == "ollama_local" for chain in ROLE_CHAINS.values() for p, _ in chain if available(p))


def describe():
    """Какие модели будут использоваться — для начала отчёта."""
    roles = {}
    for role in ROLE_CHAINS:
        ch = chain_for(role)
        roles[role] = f"{ch[0][0]}:{ch[0][1]}" if ch else "нет доступной модели"
    return roles
