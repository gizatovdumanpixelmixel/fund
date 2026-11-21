# -*- coding: utf-8 -*-
"""
ПАНЕЛЬ ФОНДА — фаза 1a (бумага).

Страница в браузере вместо командной строки:
  - запуск анализа (семь агентов из pipeline.py) кнопкой
  - отчёт последнего спринта с карточками сделок
  - открытие и проверка бумажных позиций
  - статистика закрытых сделок

Файл лежит В ТОЙ ЖЕ ПАПКЕ, что pipeline.py, prompts.py, positions.py.
Сам код агентов панель не меняет: она запускает pipeline.py как есть
и читает те же файлы (portfolio.json, results.jsonl, briefings/).

Запуск:
  python3 -m streamlit run app.py
"""

import os, sys, json, glob, subprocess, datetime, urllib.request

import streamlit as st

# ── настройки (держите такими же, как в pipeline.py / positions.py) ─────────
MAX_HEAT_PCT = 4.0
MIN_TRADES_FOR_STATS = 20          # меньше — выборка слишком мала для выводов
STALE_BRIEFING_HOURS = 8           # старше — цены входа могли уйти
OLLAMA_URL = "http://localhost:11434"

BASE = os.path.dirname(os.path.abspath(__file__))
BRIEFINGS_GLOB = os.path.join(BASE, "briefings", "briefing_*.json")
PIPELINE_PATH = os.path.join(BASE, "pipeline.py")

# Streamlit читает текст между двумя «$» как формулу: «$10.3 ... $48.62» превращалось
# в математику. Экранируем «$» во всех текстовых элементах один раз за сессию.
def _escape_dollars(text):
    return text.replace("\\$", "$").replace("$", "\\$") if isinstance(text, str) else text


def _patch_dollars():
    from streamlit.delta_generator import DeltaGenerator
    if getattr(DeltaGenerator, "_fund_dollars_patched", False):
        return
    for name in ("markdown", "caption", "success", "info", "warning", "error"):
        method = getattr(DeltaGenerator, name)

        def wrapped(self, body, *args, _m=method, **kwargs):
            return _m(self, _escape_dollars(body), *args, **kwargs)
        setattr(DeltaGenerator, name, wrapped)
        # st.caption и др. — заранее привязанные методы главного контейнера: их тоже заменяем
        setattr(st, name, getattr(st._main, name))
    DeltaGenerator._fund_dollars_patched = True


_patch_dollars()

DIRECTIONS = {
    "any": ("Любое", "3", "и покупки, и продажи — что даёт рынок"),
    "long": ("Long — покупки", "1", "ставка на рост"),
    "short": ("Short — продажи", "2", "ставка на падение"),
}


# ── данные: общие функции из positions.py (одни правила для панели и терминала) ──

import positions as P
import memory as MEM
import monitor as MON

load_portfolio = P.load_portfolio
load_results = P.load_results
compute_stats = P.compute_stats


def briefing_files():
    return sorted(glob.glob(BRIEFINGS_GLOB))


def load_briefing(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def briefing_time(path):
    """Время спринта из имени файла briefing_YYYYMMDD_HHMMSS.json."""
    stamp = os.path.basename(path)[len("briefing_"):-len(".json")]
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    except ValueError:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path))


def source_key(sprint_id, cand_id):
    return f"{sprint_id}:{cand_id}"


# ── запуск агентов ───────────────────────────────────────────────────────────

def uses_local_ollama():
    try:
        with open(PIPELINE_PATH, encoding="utf-8") as f:
            return "localhost:11434" in f.read()
    except OSError:
        return False


def ollama_running():
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2)
        return True
    except Exception:                                # noqa: BLE001
        return False


def run_pipeline(direction, on_lines):
    """Запускает pipeline.py отдельным процессом, отвечает за вас на вопрос
    о направлении и по мере работы отдаёт строки вывода в on_lines(lines)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-u", PIPELINE_PATH], cwd=BASE, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    proc.stdin.write(DIRECTIONS[direction][1] + "\n")
    proc.stdin.close()
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))
        on_lines(lines)
    return proc.wait(), lines


def progress_of(lines):
    """Доля пройденного пути по строкам '•' из вывода pipeline.py."""
    total = 9
    for ln in lines:
        if "Сканируем:" in ln:
            total = len(ln.split("Сканируем:")[1].split("|")[0].split(",")) + 5
    done = sum(1 for ln in lines if ln.strip().startswith("•"))
    last = next((ln.strip(" •") for ln in reversed(lines) if ln.strip().startswith("•")), "подготовка")
    return min(done / total, 1.0), last


# ── отрисовка ────────────────────────────────────────────────────────────────

SENT_RU = {"positive": "позитивное", "negative": "негативное", "neutral": "нейтральное", "mixed": "смешанное"}
SENT_ICON = {"positive": "🟢", "negative": "🔴", "neutral": "⚪", "mixed": "🟡"}


def render_market_news(view):
    """Разбор новостей по каждой монете — показываем всегда, даже без сделок."""
    if not view:
        return
    st.markdown("#### Новости и настроение рынка")
    for sym, v in view.items():
        sent = v.get("sentiment")
        title = (f"{SENT_ICON.get(sent, '⚪')} {sym} — настроение {SENT_RU.get(sent, sent or 'не определено')}"
                 f" · новостей за 48 ч: {v.get('news_found', 0)}"
                 + (" · ⚠ риск события" if v.get("event_risk") else ""))
        with st.expander(title):
            if v.get("has_catalyst"):
                st.markdown(f"**Катализатор:** {v['catalyst']}")
            if v.get("event_risk"):
                st.markdown(f"**⚠ Риск события:** {v['event_risk']}")
            for c in v.get("claims", []):
                st.markdown(f"- {c['text']} " + " ".join(f"[{r}]" for r in c.get("refs", [])))
            for s in v.get("sources", []):
                st.markdown(f"[{s['n']}] [{s['source']}: {s['title']}]({s['link']}) · {s.get('published', '')}")
            for n in v.get("notes", []):
                st.caption(f"ⓘ {n}")
            if not v.get("news_found"):
                st.caption("Свежих новостей по монете за 48 часов нет.")


def render_briefing(data, allow_open):
    package = data.get("package") or {}
    briefing = data.get("briefing") or {}
    sprint_id = package.get("sprint_id", "")

    rejected = package.get("rejected_candidates") or []
    if rejected:
        with st.expander(f"Снято проверкой: {len(rejected)}"):
            for x in rejected:
                st.markdown(f"- **{x['instrument']}** ({'лонг' if x['direction'] == 'long' else 'шорт'}) — "
                            f"{x['rejected_by']}: {x['reason']}")

    render_market_news(package.get("market_news") or {})

    if package.get("no_trade") or not package.get("candidates"):
        st.info("**Сделок нет.** Пустой спринт — нормальный результат.")
        reason = package.get("no_trade_reason") or briefing.get("summary", "")
        for part in [x.strip() for x in reason.split(" | ") if x.strip()]:
            st.markdown(f"- {part}")
        return

    reg = package.get("market_regime") or {}
    fng = package.get("fear_greed") or {}
    if reg.get("note") or fng:
        st.caption(" · ".join(x for x in [
            reg.get("note", "").capitalize(),
            f"индекс страха/жадности {fng['value']} ({fng.get('label', '')})" if fng else ""] if x))
    if briefing.get("summary"):
        st.markdown(briefing["summary"])

    plain = {c.get("id"): c for c in briefing.get("candidates_plain", [])}
    portfolio = load_portfolio()
    opened_sources = {x.get("source") for x in portfolio["open_positions"]}
    heat = sum(x.get("risk_pct", 0) for x in portfolio["open_positions"])

    for card in package["candidates"]:
        c = plain.get(card["id"], {})
        side = "КУПИТЬ (лонг)" if card["direction"] == "long" else "ПРОДАТЬ в шорт"
        with st.container(border=True):
            st.subheader(f"{card['instrument']} — {side}")
            if c.get("what"):
                st.markdown(c["what"])
            row1 = st.columns(3)
            row1[0].metric("Вход", f"~{card['entry']}")
            row1[1].metric("Стоп", card["stop"])
            row1[2].metric("Цель", card.get("target_far") or " / ".join(str(t) for t in card.get("targets", [])))
            row2 = st.columns(3)
            row2[0].metric("Размер", f"{card['size']['value']} {card['size']['unit_type']}")
            row2[1].metric(f"Риск ({card.get('risk_pct')}% капитала)", f"${card.get('risk_amount')}")
            row2[2].metric("R после издержек", card.get("expected_R_net"))

            risk_amt, r_net = card.get("risk_amount"), card.get("expected_R_net")
            if risk_amt is not None and r_net is not None:
                st.caption(f"Если цену выбьет по стопу, потеряете около ${risk_amt}. "
                           f"Цель по прибыли — около ${round(risk_amt * r_net, 2)}. "
                           f"Срок: {card.get('hold_estimate', '')}.")
            if card.get("size_note"):
                st.caption(f"ⓘ {card['size_note']}")

            if c.get("why"):
                st.markdown(f"**Почему:** {c['why']}")
            if card.get("news_sources"):
                st.markdown("**Источники новостей:** " + " · ".join(
                    f"[{s['n']}] [{s['source']}: {s['title']}]({s['link']})" for s in card["news_sources"]))
            for note in card.get("notes", []):
                st.caption(f"ⓘ {note}")
            if c.get("what_could_go_wrong"):
                st.markdown(f"**Что может пойти не так:** {c['what_could_go_wrong']}")
            debate = next((o for o in ((data.get("report_2") or {}).get("clean_options") or [])
                           if o.get("id") == card["id"]), None)
            if debate and debate.get("long_case"):
                with st.expander("Дебаты: команда лонг против команды шорт"):
                    for side, title in (("long_case", "За рост"), ("short_case", "За падение")):
                        case = debate.get(side) or {}
                        st.markdown(f"**{title}:** {case.get('strongest', '')}")
                        for arg in case.get("arguments", []):
                            mark = " _(без цифр — риторика)_" if arg.get("rhetoric") else ""
                            st.markdown(f"- {arg.get('text', '')}{mark}")
                    st.markdown(f"**Судья:** победила сторона «{ {'long': 'лонг', 'short': 'шорт'}.get(debate.get('winner'), 'никто') }». "
                                f"{debate.get('reason', '')}")
            left, right = st.columns(2)
            left.markdown(f"**Эксперт №1:** {c.get('expert_said', '—')}")
            right.markdown(f"**Консультант №2:** {c.get('consultant_said', '—')}")
            if c.get("agreement"):
                st.markdown(f"→ {c['agreement']}")

            if not allow_open:
                continue
            key = source_key(sprint_id, card["id"])
            if key in opened_sources:
                st.success("Позиция по этой карточке уже записана.")
            elif heat + card.get("risk_pct", 0) > MAX_HEAT_PCT + 1e-9:
                st.warning(f"Открыть нельзя: занятый риск стал бы "
                           f"{round(heat + card.get('risk_pct', 0), 2)}% при лимите {MAX_HEAT_PCT}%.")
            elif st.button("Я вхожу в эту сделку — проверить и записать", key=f"open_{key}"):
                try:
                    price = P.price_now(card["instrument"])
                except Exception as e:                       # noqa: BLE001
                    st.error(f"Не удалось получить текущую цену: {e}")
                else:
                    check = P.preflight(card, P.load_portfolio(), price)
                    if check["ok"]:
                        pid = P.record_position(card, sprint_id, check)
                        text = (f"Записана позиция {pid} по {card['instrument']} по цене {check['fill']}: "
                                f"чистая R {check['R_net']}, риск ${check['risk_amount']}.")
                        if check["notes"]:
                            text += " " + " ".join(check["notes"])
                        st.session_state["flash"] = ("success", text)
                        st.rerun()
                    else:
                        st.error("Позиция не записана. Предвходовая проверка по цене "
                                 f"{price}:\n\n" + "\n".join(f"- {r}" for r in check["reasons"]))

    if briefing.get("terms_used"):
        with st.expander("Новые слова"):
            for t in briefing["terms_used"]:
                st.markdown(f"- **{t.get('term', '')}** — {t.get('plain', '')}")


STEP_RU = {"tech": "Технический аналитик", "news": "Аналитик новостей", "risk_calc": "Риск-движок (код)",
           "strategist": "Трейдер-стратег", "debate_long": "Адвокат лонга", "debate_short": "Адвокат шорта",
           "judge": "Консультант №2 — судья", "report_1": "Эксперт №1", "translator": "Переводчик"}


def render_run_log(sprint_id):
    """Журнал прогона: что ответил каждый агент (logs/run_<id>.jsonl)."""
    path = os.path.join(BASE, "logs", f"run_{sprint_id}.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    with st.expander(f"Журнал работы агентов ({len(rows)} шагов)"):
        st.caption(f"Файл: logs/run_{sprint_id}.jsonl — полный вход и выход каждого агента.")
        for r in rows:
            step = r.get("step", "")
            kind, _, sym = step.partition("::")
            if sym.endswith("cjk_retry") or kind.endswith("cjk_retry"):
                continue
            name = STEP_RU.get(kind, kind)
            out = r.get("output") or {}
            if "error" in out:
                st.error(f"{r.get('ts', '')[11:19]} · {name} {sym}: ошибка — {out['error']}")
                continue
            summary = (out.get("notes") or out.get("catalyst") or out.get("strongest") or out.get("reason")
                       or out.get("expert_opinion") or out.get("summary") or "; ".join(out.get("reasons", []))
                       or ("прошёл гейты" if out.get("passed") else ""))
            st.markdown(f"**{r.get('ts', '')[11:19]} · {name}** {sym}  \n{summary}")
            with st.popover("подробно"):
                st.json(out)


def tab_analysis():
    st.caption("Семь агентов изучают рынок и готовят идеи. Денег не тратит: это бумага.")
    direction = st.radio(
        "Направление анализа", list(DIRECTIONS), horizontal=True,
        format_func=lambda d: DIRECTIONS[d][0],
        captions=[DIRECTIONS[d][2] for d in DIRECTIONS])

    if st.button("Запустить анализ", type="primary"):
        if not os.path.exists(PIPELINE_PATH):
            st.error("Не нахожу pipeline.py. Файл app.py должен лежать в одной папке с ним.")
        elif uses_local_ollama() and not ollama_running():
            st.error("Ollama не запущена. Откройте приложение Ollama и нажмите кнопку ещё раз.")
        else:
            before = set(briefing_files())
            with st.status("Агенты работают. Обычно это несколько минут — "
                           "не нажимайте другие кнопки, пока идёт анализ.", expanded=True) as status:
                bar = st.progress(0.0, text="подготовка")
                log_box = st.empty()

                def on_lines(lines):
                    frac, last = progress_of(lines)
                    bar.progress(frac, text=last)
                    log_box.code("\n".join(lines[-12:]))

                code, lines = run_pipeline(direction, on_lines)
                new_files = set(briefing_files()) - before
                if code == 0 and new_files:
                    bar.progress(1.0, text="готово")
                    status.update(label="Анализ завершён", state="complete", expanded=False)
                else:
                    status.update(label="Анализ завершился с ошибкой", state="error", expanded=True)
                    log_box.code("\n".join(lines[-40:]))
            if code == 0 and new_files:
                st.rerun()

    files = briefing_files()
    if not files:
        st.info("Отчётов пока нет. Запустите первый анализ.")
        return
    latest = files[-1]
    when = briefing_time(latest)
    age_h = (datetime.datetime.now() - when).total_seconds() / 3600
    st.divider()
    st.markdown(f"### Последний отчёт — {when:%d.%m.%Y %H:%M}")
    if age_h > STALE_BRIEFING_HOURS:
        age_text = f"{int(age_h)} ч." if age_h < 48 else f"{int(age_h // 24)} дн."
        st.warning(f"Отчёту {age_text} Цены входа могли уйти — "
                   f"перед открытием позиции лучше запустить анализ заново.")
    data = load_briefing(latest)
    render_briefing(data, allow_open=True)
    render_run_log((data.get("package") or {}).get("sprint_id", ""))


def tab_positions():
    p = load_portfolio()
    positions = p["open_positions"]
    if not positions:
        st.info("Открытых позиций нет.")
    else:
        st.dataframe([{
            "ID": x["id"], "Инструмент": x["instrument"],
            "Сторона": "лонг" if x["direction"] == "long" else "шорт",
            "Вход": x["entry"], "Стоп": x["stop"],
            "Цель": x.get("target_far") or " / ".join(str(t) for t in x.get("targets", [])),
            "Размер": f"{x['size']['value']} {x['size']['unit_type']}",
            "Риск $": x.get("risk_amount"), "Риск %": x.get("risk_pct"),
            "Прогноз R": x.get("expected_R_net"),
            "Открыта": x["opened_at"][:16].replace("T", " "),
        } for x in positions], hide_index=True, width="stretch")

        st.caption("Позиция держится до стопа или до дальней цели. Проверка идёт по истории "
                   "часовых свечей с момента открытия: стоп или цель, задетые при выключенном "
                   "ноутбуке, будут учтены. Если в одной свече задеты и стоп, и цель — засчитывается стоп.")
        if st.button("Проверить позиции", type="primary"):
            with st.spinner("Сверяюсь с рынком..."):
                st.session_state["check_messages"] = P.check_all()
            st.rerun()

    for kind, text in st.session_state.get("check_messages", []):
        getattr(st, kind)(text)

    if positions:
        st.divider()
        st.markdown("### Монитор: держать, пересмотреть или закрыть")
        st.caption("Вердикт ставят правила в коде: разворот тренда или режима рынка против позиции, "
                   "близость стопа, срок удержания, прибыль от +1R, толпа на фьючерсах. "
                   "Монитор только советует — позиции и стопы он не меняет.")
        if st.button("Запустить монитор"):
            with st.spinner("Оцениваю позиции..."):
                try:
                    st.session_state["monitor"] = MON.run_once()
                except Exception as e:                   # noqa: BLE001
                    st.session_state["monitor"] = ([], [{"id": "", "instrument": "", "direction": "",
                                                          "verdict": "REVIEW", "verdict_ru": "ОШИБКА",
                                                          "reasons": [str(e)]}])
            st.rerun()
    closed, rows = st.session_state.get("monitor", ([], []))
    for _kind, text in closed:
        st.success(f"Закрыта: {text}")
    style = {"CLOSE": st.error, "REVIEW": st.warning, "HOLD": st.success}
    for r in rows:
        head = f"**{r['verdict_ru']}** — {r['id']} {r['instrument']}"
        if r.get("price") is not None:
            head += (f": цена {r['price']}, результат {r['progress_R']} R, "
                     f"до стопа {r['to_stop_R']} R, {r['days_open']} дн.")
        body = "\n".join(f"- {x}" for x in r["reasons"])
        style.get(r["verdict"], st.info)(head + ("\n\n" + body if body else ""))


def tab_stats():
    rows = load_results()
    if not rows:
        st.info("Закрытых сделок пока нет — статистики нет.")
        section_learning()
        return
    s = compute_stats(rows)
    cols = st.columns(5)
    cols[0].metric("Сделок", s["n"])
    cols[1].metric("Винрейт", f"{s['win_rate']}%", f"{s['wins']} прибыль / {s['losses']} убыток",
                   delta_color="off")
    cols[2].metric("Средняя прибыль / убыток", f"{s['avg_win']} R / {s['avg_loss']} R")
    cols[3].metric("Матожидание", f"{s['expectancy']} R", "главный показатель", delta_color="off")
    cols[4].metric("Итог", f"${s['total_pnl']}")

    if s["n"] < MIN_TRADES_FOR_STATS:
        st.warning(f"Сделок {s['n']} — это меньше {MIN_TRADES_FOR_STATS}. "
                   f"Выборка слишком мала: выводы о системе по ней делать рано.")
    elif s["expectancy"] > 0:
        st.success("Матожидание выше нуля — система в плюсе на этой выборке.")
    else:
        st.error("Матожидание не выше нуля — на этой выборке система не зарабатывает.")
    st.caption("Винрейт сам по себе обманчив — смотрите вместе с матожиданием.")

    cumulative, total = [], 0.0
    for r in rows:
        total += r["actual_R"]
        cumulative.append(round(total, 2))
    st.markdown("**Накопленный результат, R**")
    st.line_chart({"Накопленный R": cumulative})

    st.dataframe([{
        "ID": r["id"], "Инструмент": r["instrument"],
        "Сторона": "лонг" if r["direction"] == "long" else "шорт",
        "Вход": r.get("entry"), "Выход": r.get("exit"), "Итог": r.get("hit"),
        "Прогноз R": r.get("forecast_R_net"), "Факт R": r["actual_R"], "$": r.get("pnl"),
        "Закрыта": str(r.get("closed_at", ""))[:16].replace("T", " "),
    } for r in reversed(rows)], hide_index=True, width="stretch")
    section_learning()


def section_learning():
    st.divider()
    st.markdown("### Чему научилась система")
    if st.button("Разобрать новые сделки и отклонённые сетапы"):
        with st.spinner("Сверяюсь с историей рынка..."):
            try:
                nl, nr = MEM.review_closed(), MEM.review_rejected()
                st.session_state["flash"] = ("success", f"Новых уроков: {len(nl)}, "
                                             f"разобрано отклонённых сетапов: {len(nr)}.")
            except Exception as e:                   # noqa: BLE001
                st.session_state["flash"] = ("error", f"Разбор не удался: {e}")
        st.rerun()
    lessons = MEM.load(MEM.LESSONS_PATH)
    if lessons:
        for l in reversed(lessons[-10:]):
            tags = f"  _[{', '.join(l['tags'])}]_" if l.get("tags") else ""
            st.markdown(f"- {l['what_happened']}{tags}\n  \n  → **{l['rule']}**")
    else:
        st.info("Уроков пока нет. Они появятся после разбора закрытых сделок.")
    rejected = MEM.load(MEM.REJECTED_PATH)
    if rejected:
        saved = sum(1 for r in rejected if r["outcome"] == "would_stop")
        missed = sum(1 for r in rejected if r["outcome"] == "would_target")
        st.markdown(f"**Отклонённые сетапы:** гейты были правы {saved} раз, "
                    f"отсекли прибыльную сделку {missed} раз.")
    st.markdown("**Табель: кто из участников чаще прав**")
    st.dataframe([{"Участник": r["agent"], "Решений": r["n"], "Верно": r["correct"],
                   "Точность": f"{r['accuracy_pct']}%" if r["accuracy_pct"] is not None else "—",
                   "": r["note"]} for r in MEM.scorecard()], hide_index=True, width="stretch")
    st.caption(f"Пока решений меньше {MEM.MIN_DECISIONS_FOR_SCORE}, точность ничего не доказывает.")


def tab_history():
    files = briefing_files()
    if not files:
        st.info("Отчётов пока нет.")
        return
    choice = st.selectbox("Отчёт", list(reversed(files)),
                          format_func=lambda f: f"{briefing_time(f):%d.%m.%Y %H:%M}")
    data = load_briefing(choice)
    render_briefing(data, allow_open=False)
    render_run_log((data.get("package") or {}).get("sprint_id", ""))


def main():
    st.set_page_config(page_title="Фонд — панель", page_icon="📊", layout="wide")
    st.title("Фонд — панель управления")
    st.caption("Фаза 1a: бумажная торговля. Реальные сделки не исполняются, решение за вами.")

    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])

    p = load_portfolio()
    rows = load_results()
    heat = round(sum(x.get("risk_pct", 0) for x in p["open_positions"]), 2)
    s = compute_stats(rows)
    cols = st.columns(4)
    cols[0].metric("Капитал (бумага)", f"${p['capital']:,}".replace(",", " "))
    cols[1].metric("Открытых позиций", len(p["open_positions"]))
    cols[2].metric(f"Занятый риск (лимит {MAX_HEAT_PCT}%)", f"{heat}%")
    cols[3].metric("Закрытых сделок", s["n"])
    if s["n"]:
        cols[3].caption(f"матожидание {s['expectancy']} R")

    t1, t2, t3, t4 = st.tabs(["Новый анализ", "Позиции", "Статистика", "История отчётов"])
    with t1:
        tab_analysis()
    with t2:
        tab_positions()
    with t3:
        tab_stats()
    with t4:
        tab_history()


if __name__ == "__main__":
    main()
