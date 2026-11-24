// Мгновенный приёмник команд для бота фонда (Cloudflare Worker, бесплатный тариф).
//
// Telegram присылает сообщение сюда сразу (webhook). Worker:
//   • отвечает за секунду («принял», меню, кнопки выбора режима анализа);
//   • /status отвечает сам, читая portfolio.json и control.json из репозитория;
//   • остальные команды кладёт в очередь (KV) и запускает в GitHub задачу «Команды и монитор»,
//     которая выполняет команду и присылает результат (обычно 1–2 мин, анализ — 4–6 мин).
// Если запуск задачи сорвался, команда остаётся в очереди и выполнится в ближайший часовой цикл.
//
// Нужно в настройках Worker'а:
//   Secrets:   TELEGRAM_TOKEN, GH_TOKEN, BOT_SECRET
//   Variables: OWNER_CHAT_ID
//   KV binding: QUEUE

const REPO = "gizatovdumanpixelmixel/fund";
const WORKFLOW = "hourly.yml";

const MENU = {
  "📊 Статус": "/status",
  "▶️ Анализ": "/run",
  "🔍 Проверить позиции": "/check",
  "🗓 Неделя": "/week",
  "🟢 Работать": "/work",
  "😴 Отдыхать": "/rest",
  "❓ Помощь": "/help",
};
const KEYBOARD = {
  keyboard: [["📊 Статус", "▶️ Анализ"], ["🔍 Проверить позиции", "🗓 Неделя"],
             ["🟢 Работать", "😴 Отдыхать"], ["❓ Помощь"]],
  resize_keyboard: true, is_persistent: true,
};
const RUN_BUTTONS = {
  inline_keyboard: [[
    { text: "↕️ Любое", callback_data: "run:any" },
    { text: "📈 Лонг", callback_data: "run:long" },
    { text: "📉 Шорт", callback_data: "run:short" },
  ]],
};
const DIR_RU = { any: "любое направление", long: "лонг (рост)", short: "шорт (падение)" };

const HELP =
  "Команды (кнопки внизу делают то же самое):\n" +
  "/status — состояние (ответ сразу)\n" +
  "/run — анализ сейчас, направление выберете кнопкой\n" +
  "/check — проверить позиции\n" +
  "/open C-0001 — записать вход в идею из последнего отчёта\n" +
  "/close P-0001 — закрыть позицию по текущей цене\n" +
  "/week — сводка за неделю\n" +
  "/work — работать по расписанию, /rest — отдыхать\n\n" +
  "Ответ на команды — обычно за 1–2 минуты, анализ — за 4–6 минут.";

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    try {
      if (url.pathname === "/webhook" && req.method === "POST") {
        if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.BOT_SECRET)
          return new Response("forbidden", { status: 403 });
        const upd = await req.json();
        ctx.waitUntil(onUpdate(upd, env).catch((e) => console.log("update error:", e && e.stack)));
        return new Response("ok");
      }
      if (url.pathname === "/queue") return await drain(req, env);
      if (url.pathname === "/setup") return await setup(url, env);
      return new Response("fund bot: работает");
    } catch (e) {
      return new Response("error: " + e.message, { status: 500 });
    }
  },
};

// ── Telegram ────────────────────────────────────────────────────────────────

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json();
}

const say = (env, text, extra = {}) =>
  tg(env, "sendMessage", { chat_id: env.OWNER_CHAT_ID, text, ...extra });

async function onUpdate(upd, env) {
  const owner = String(env.OWNER_CHAT_ID);

  if (upd.callback_query) {
    const q = upd.callback_query;
    if (String(q.message?.chat?.id) !== owner) return;
    const [kind, arg] = String(q.data || "").split(":");
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id });
    if (kind === "run" && DIR_RU[arg]) {
      const ok = await enqueue(env, `/run ${arg}`);
      await tg(env, "editMessageText", {
        chat_id: owner, message_id: q.message.message_id,
        text: ok ? `⏳ Анализ запущен: ${DIR_RU[arg]}. Отчёт придёт через 4–6 минут.`
                 : `⚠️ Не смог запустить сразу — анализ (${DIR_RU[arg]}) выполнится в ближайший часовой цикл.`,
      });
    }
    return;
  }

  const m = upd.message;
  if (!m || !m.text) return;
  if (String(m.chat.id) !== owner) {
    await tg(env, "sendMessage", { chat_id: m.chat.id, text: "Это личный бот, команды принимаются только от владельца." });
    return;
  }
  let text = (MENU[m.text.trim()] || m.text).trim();
  const [rawCmd, ...args] = text.split(/\s+/);
  const cmd = rawCmd.toLowerCase().split("@")[0];

  if (cmd === "/start" || cmd === "/help") return say(env, HELP, { reply_markup: KEYBOARD });
  if (cmd === "/status") return say(env, await statusText(env));
  if (cmd === "/run" && !args.length)
    return say(env, "В какую сторону искать сделки?", { reply_markup: RUN_BUTTONS });
  if (!cmd.startsWith("/")) return say(env, "Не понял. Нажмите кнопку внизу или /help.", { reply_markup: KEYBOARD });

  const ok = await enqueue(env, `${cmd} ${args.join(" ")}`.trim());
  const eta = cmd === "/run" ? "4–6 минут" : "1–2 минуты";
  return say(env, ok ? `⏳ Принял «${text}». Ответ — через ${eta}.`
                     : `⚠️ Принял «${text}», но не смог запустить сразу — выполню в ближайший часовой цикл.`);
}

// ── очередь команд и запуск задачи в GitHub ───────────────────────────────────

async function enqueue(env, text) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  await env.QUEUE.put(`cmd:${id}`, JSON.stringify({ id, text, ts: new Date().toISOString() }),
                      { expirationTtl: 3 * 24 * 3600 });
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: gh(env, { "content-type": "application/json" }),
    body: JSON.stringify({ ref: "main", inputs: { cmd: text, cmd_id: id } }),
  });
  if (r.status !== 204) console.log("dispatch failed", r.status, await r.text());
  return r.status === 204;
}

async function drain(req, env) {          // задача в GitHub забирает накопившиеся команды
  if (req.headers.get("X-Bot-Key") !== env.BOT_SECRET) return new Response("forbidden", { status: 403 });
  const list = await env.QUEUE.list({ prefix: "cmd:" });
  const items = [];
  for (const k of list.keys) {
    const v = await env.QUEUE.get(k.name);
    if (v) items.push(JSON.parse(v));
    await env.QUEUE.delete(k.name);
  }
  items.sort((a, b) => a.ts.localeCompare(b.ts));
  return Response.json({ items });
}

const gh = (env, extra = {}) => ({
  authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json",
  "x-github-api-version": "2022-11-28", "user-agent": "fund-bot-worker", ...extra,
});

async function repoJson(env, path) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}?ref=main`,
                        { headers: gh(env, { accept: "application/vnd.github.raw+json" }) });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

// ── /status сразу, без запуска задачи ────────────────────────────────────────

async function statusText(env) {
  try {
    const [p, c] = await Promise.all([repoJson(env, "portfolio.json"), repoJson(env, "control.json")]);
    const lines = [`Режим: ${c.mode === "rest" ? "😴 отдыхаю" : "🟢 работаю"}`,
                   `Открытых позиций: ${p.open_positions.length}, занятый риск ${p.current_heat_pct || 0}%`];
    for (const x of p.open_positions) {
      lines.push(`  ${x.id} ${x.instrument} ${x.direction === "long" ? "лонг" : "шорт"}` +
                 `${x.mode === "bounce" ? " 🔄" : ""} вход ${x.entry} стоп ${x.stop} цель ${x.target_far}`);
    }
    if (c.last_monitor) lines.push(`Последняя проверка позиций: ${c.last_monitor.replace("T", " ").slice(0, 16)}`);
    lines.push("Текущие цены и подробности — кнопка «🔍 Проверить позиции».");
    return lines.join("\n");
  } catch (e) {
    return `Не смог прочитать состояние из GitHub (${e.message}). Проверьте GH_TOKEN.`;
  }
}

// ── разовая настройка: открыть https://<worker>/setup?key=<BOT_SECRET> ──────

async function setup(url, env) {
  if (url.searchParams.get("key") !== env.BOT_SECRET) return new Response("forbidden", { status: 403 });
  const missing = ["TELEGRAM_TOKEN", "GH_TOKEN", "BOT_SECRET", "OWNER_CHAT_ID", "QUEUE"].filter((k) => !env[k]);
  if (missing.length) return new Response("Не заданы: " + missing.join(", "), { status: 400 });
  const hook = await tg(env, "setWebhook", {
    url: `${url.origin}/webhook`, secret_token: env.BOT_SECRET,
    allowed_updates: ["message", "callback_query"],
  });
  const cmds = await tg(env, "setMyCommands", { commands: [
    { command: "status", description: "состояние (сразу)" },
    { command: "run", description: "анализ сейчас — выбор направления" },
    { command: "check", description: "проверить позиции" },
    { command: "week", description: "сводка за неделю" },
    { command: "work", description: "работать по расписанию" },
    { command: "rest", description: "отдыхать" },
    { command: "help", description: "все команды" },
  ] });
  let ghCheck = "ok";
  try { await repoJson(env, "control.json"); } catch (e) { ghCheck = e.message; }
  if (hook.ok) await say(env, "⚡ Бот переключён на мгновенные ответы. Кнопки — внизу.", { reply_markup: KEYBOARD });
  return Response.json({ webhook: hook, commands: cmds.ok, github: ghCheck });
}
