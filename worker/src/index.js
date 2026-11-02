/**
 * Bot de Telegram del escáner (Cloudflare Worker).
 *
 * - Recibe los mensajes de Telegram por webhook (POST /telegram).
 * - Las personas nuevas quedan pendientes hasta que el administrador las aprueba.
 * - Cada persona aprobada elige de 1 a 4 estrategias y sus activos con botones.
 * - El escáner (GitHub Actions) pide la lista con GET /api/subscribers.
 *
 * Secretos (wrangler secret put): BOT_TOKEN, WEBHOOK_SECRET, API_KEY, ADMIN_ID.
 * KV: USERS, una clave "u:<chat_id>" por persona.
 */
import CATALOG from "./catalog.json";

const ASSETS = [
  ["BTC/USD", "₿ BTC"],
  ["XAU/USD", "🥇 Oro"],
];
const MAX_STRATEGIES = 4;
const DISCLAIMER = "_Son guías para apoyar tu análisis, no garantías ni asesoría financiera._";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json();
      try {
        await handle(update, env);
      } catch (err) {
        console.log("error", err && err.stack);
      }
      return new Response("ok");
    }
    if (url.pathname === "/api/subscribers") {
      if (request.headers.get("Authorization") !== `Bearer ${env.API_KEY}`) {
        return new Response("unauthorized", { status: 401 });
      }
      const users = await allUsers(env);
      const subscribers = users
        .filter((u) => u.status === "approved" && !u.paused && u.strategies.length)
        .map((u) => ({ id: u.id, name: u.name, strategies: u.strategies, assets: u.assets }));
      return Response.json({ subscribers });
    }
    return new Response("Bot de señales activo.");
  },
};

// ------------------------------------------------------------------------- //
// Telegram y almacenamiento
// ------------------------------------------------------------------------- //
async function tg(env, method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!body.ok) console.log(method, "falló:", body.description);
  return body;
}

const send = (env, chatId, text, extra = {}) =>
  tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "Markdown", disable_web_page_preview: true, ...extra });

const edit = (env, msg, text, extra = {}) =>
  tg(env, "editMessageText", {
    chat_id: msg.chat.id, message_id: msg.message_id, text, parse_mode: "Markdown",
    disable_web_page_preview: true, ...extra,
  });

async function getUser(env, id) {
  const raw = await env.USERS.get(`u:${id}`);
  return raw ? JSON.parse(raw) : null;
}

const putUser = (env, u) => env.USERS.put(`u:${u.id}`, JSON.stringify(u));

async function allUsers(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.USERS.list({ prefix: "u:", cursor });
    for (const k of page.keys) {
      const u = await env.USERS.get(k.name);
      if (u) out.push(JSON.parse(u));
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

// Markdown clásico de Telegram: escapar lo que escriben las personas (nombres)
const esc = (s) => String(s || "").replace(/([_*`\[])/g, "\\$1");

const isAdmin = (env, id) => String(id) === String(env.ADMIN_ID);

function newUser(from) {
  return {
    id: String(from.id),
    name: [from.first_name, from.last_name].filter(Boolean).join(" ") || "Sin nombre",
    username: from.username || "",
    status: "pending",
    paused: false,
    strategies: [],
    assets: ASSETS.map((a) => a[0]),
    draft: null,
    created: new Date().toISOString(),
  };
}

// ------------------------------------------------------------------------- //
// Menús
// ------------------------------------------------------------------------- //
function strategyMenu(selected) {
  const lines = [
    `🧩 *Escoge de 1 a ${MAX_STRATEGIES} estrategias* con las que más cómodo te sientas.`,
    "Toca para marcar ✅ y luego *Siguiente*.",
    "",
  ];
  CATALOG.forEach((s, i) => {
    lines.push(`*${i + 1}. ${s.emoji} ${s.name}* (velas de ${s.timeframe})`);
    lines.push(s.description);
    lines.push(`📊 ${s.stats}`);
    lines.push("");
  });
  lines.push(DISCLAIMER);
  const keyboard = CATALOG.map((s, i) => [{
    text: `${selected.includes(s.id) ? "✅" : "⬜"} ${i + 1}. ${s.emoji} ${s.name}`,
    callback_data: `st:${s.id}`,
  }]);
  keyboard.push([{ text: `Siguiente ➡️ (${selected.length}/${MAX_STRATEGIES})`, callback_data: "st_ok" }]);
  return { text: lines.join("\n"), reply_markup: { inline_keyboard: keyboard } };
}

function assetMenu(selected) {
  const keyboard = ASSETS.map(([id, label]) => [{
    text: `${selected.includes(id) ? "✅" : "⬜"} ${label}`, callback_data: `as:${id}`,
  }]);
  keyboard.push([{ text: "Guardar ✅", callback_data: "as_ok" }]);
  return {
    text: "🪙 *¿De qué activos quieres recibir señales?*\nToca para marcar y luego *Guardar*.",
    reply_markup: { inline_keyboard: keyboard },
  };
}

const strategyNames = (ids) =>
  ids.map((id) => CATALOG.find((s) => s.id === id)).filter(Boolean).map((s) => `${s.emoji} ${s.name}`).join(", ");
const assetNames = (ids) => ASSETS.filter((a) => ids.includes(a[0])).map((a) => a[1]).join(", ");

function statusText(u) {
  return [
    `👤 *${esc(u.name)}*`,
    `🧩 Estrategias: ${u.strategies.length ? strategyNames(u.strategies) : "ninguna todavía"}`,
    `🪙 Activos: ${assetNames(u.assets)}`,
    `🔔 Alertas: ${u.paused ? "en pausa ⏸" : "activas ▶️"}`,
  ].join("\n");
}

const HELP = [
  "📖 *Comandos*",
  "/estrategias — elegir o cambiar tus estrategias (1 a 4)",
  "/activos — elegir BTC, Oro o ambos",
  "/estado — ver tu configuración",
  "/pausa — dejar de recibir señales un tiempo",
  "/reanudar — volver a recibirlas",
].join("\n");

async function openStrategyMenu(env, u, msgToEdit) {
  u.draft = { strategies: [...u.strategies], assets: [...u.assets] };
  await putUser(env, u);
  const m = strategyMenu(u.draft.strategies);
  return msgToEdit ? edit(env, msgToEdit, m.text, { reply_markup: m.reply_markup })
    : send(env, u.id, m.text, { reply_markup: m.reply_markup });
}

// ------------------------------------------------------------------------- //
// Mensajes
// ------------------------------------------------------------------------- //
async function handle(update, env) {
  if (update.callback_query) return handleCallback(update.callback_query, env);
  const msg = update.message;
  if (!msg || !msg.text || msg.chat.type !== "private") return;
  const text = msg.text.trim();
  const cmd = text.split(/\s+/)[0].split("@")[0].toLowerCase();
  let u = await getUser(env, msg.from.id);

  if (!u) {
    u = newUser(msg.from);
    if (isAdmin(env, u.id)) {
      u.status = "approved";
      await putUser(env, u);
      if (["/usuarios", "/quitar", "/aprobar"].includes(cmd)) return adminCommand(env, cmd, text);
      await send(env, u.id, "👋 Hola, administrador. Elige tus estrategias:");
      return openStrategyMenu(env, u);
    }
    await putUser(env, u);
    await send(env, u.id,
      `👋 Hola, ${esc(u.name)}. Soy el bot de señales de *BTC y Oro*.\n\n` +
      "Para usarlo, el administrador tiene que aprobarte. Ya le avisé; te escribo apenas responda.");
    return send(env, env.ADMIN_ID,
      `🆕 *Nueva solicitud*\n${esc(u.name)}${u.username ? ` (@${esc(u.username)})` : ""}\nID: ${u.id}`,
      { reply_markup: { inline_keyboard: [[
        { text: "✅ Aprobar", callback_data: `ap:${u.id}` },
        { text: "❌ Rechazar", callback_data: `rj:${u.id}` },
      ]] } });
  }

  if (isAdmin(env, u.id) && (cmd === "/usuarios" || cmd === "/quitar" || cmd === "/aprobar")) {
    return adminCommand(env, cmd, text);
  }
  if (u.status === "pending") return send(env, u.id, "⏳ Tu solicitud sigue pendiente de aprobación.");
  if (u.status !== "approved") return send(env, u.id, "No tienes acceso a este bot.");

  switch (cmd) {
    case "/start":
    case "/estrategias":
      return openStrategyMenu(env, u);
    case "/activos": {
      u.draft = { strategies: [...u.strategies], assets: [...u.assets], onlyAssets: true };
      await putUser(env, u);
      const m = assetMenu(u.draft.assets);
      return send(env, u.id, m.text, { reply_markup: m.reply_markup });
    }
    case "/estado":
      return send(env, u.id, statusText(u));
    case "/pausa":
      u.paused = true;
      await putUser(env, u);
      return send(env, u.id, "⏸ Listo, no te mando señales hasta que escribas /reanudar.");
    case "/reanudar":
      u.paused = false;
      await putUser(env, u);
      return send(env, u.id, "▶️ Señales reanudadas.\n\n" + statusText(u));
    default:
      return send(env, u.id, HELP);
  }
}

async function adminCommand(env, cmd, text) {
  const arg = text.split(/\s+/)[1];
  if (cmd === "/usuarios") {
    const users = await allUsers(env);
    const icon = { approved: "✅", pending: "⏳", rejected: "❌" };
    const lines = users.map((u) =>
      `${icon[u.status] || "•"} ${esc(u.name)} · ${u.id}${u.paused ? " · ⏸" : ""}\n    ${strategyNames(u.strategies) || "sin estrategias"}`);
    return send(env, env.ADMIN_ID, `👥 *Personas (${users.length})*\n\n${lines.join("\n") || "Nadie todavía."}`);
  }
  const u = arg && (await getUser(env, arg));
  if (!u) return send(env, env.ADMIN_ID, `Uso: ${cmd} <ID>  (los ID salen en /usuarios)`);
  if (cmd === "/quitar") {
    u.status = "rejected";
    await putUser(env, u);
    return send(env, env.ADMIN_ID, `❌ ${esc(u.name)} ya no recibe señales.`);
  }
  u.status = "approved";
  await putUser(env, u);
  await send(env, env.ADMIN_ID, `✅ ${esc(u.name)} aprobado.`);
  await send(env, u.id, "✅ ¡Fuiste aprobado! Ahora elige tus estrategias:");
  return openStrategyMenu(env, u);
}

async function handleCallback(q, env) {
  const data = q.data || "";
  const answer = (text, alert = false) =>
    tg(env, "answerCallbackQuery", { callback_query_id: q.id, text, show_alert: alert });

  if (data.startsWith("ap:") || data.startsWith("rj:")) {
    if (!isAdmin(env, q.from.id)) return answer("Solo el administrador puede hacer esto.");
    const u = await getUser(env, data.slice(3));
    if (!u) return answer("Esa persona ya no existe.");
    const approved = data.startsWith("ap:");
    u.status = approved ? "approved" : "rejected";
    await putUser(env, u);
    await edit(env, q.message, `${approved ? "✅ Aprobado" : "❌ Rechazado"}: ${esc(u.name)} (ID ${u.id})`);
    await answer(approved ? "Aprobado" : "Rechazado");
    if (!approved) return send(env, u.id, "Tu solicitud no fue aprobada.");
    await send(env, u.id, "✅ ¡Fuiste aprobado! Ahora elige tus estrategias:");
    return openStrategyMenu(env, u);
  }

  const u = await getUser(env, q.from.id);
  if (!u || u.status !== "approved") return answer("No tienes acceso.");
  if (!u.draft) u.draft = { strategies: [...u.strategies], assets: [...u.assets] };
  const d = u.draft;

  if (data.startsWith("st:")) {
    const id = data.slice(3);
    if (d.strategies.includes(id)) d.strategies = d.strategies.filter((s) => s !== id);
    else if (d.strategies.length >= MAX_STRATEGIES) return answer(`Máximo ${MAX_STRATEGIES} estrategias.`, true);
    else d.strategies.push(id);
    await putUser(env, u);
    await answer("");
    const m = strategyMenu(d.strategies);
    return tg(env, "editMessageReplyMarkup", {
      chat_id: q.message.chat.id, message_id: q.message.message_id, reply_markup: m.reply_markup,
    });
  }
  if (data === "st_ok") {
    if (!d.strategies.length) return answer("Elige al menos 1 estrategia.", true);
    await putUser(env, u);
    await answer("");
    const m = assetMenu(d.assets);
    return edit(env, q.message, m.text, { reply_markup: m.reply_markup });
  }
  if (data.startsWith("as:")) {
    const id = data.slice(3);
    d.assets = d.assets.includes(id) ? d.assets.filter((a) => a !== id) : [...d.assets, id];
    await putUser(env, u);
    await answer("");
    const m = assetMenu(d.assets);
    return tg(env, "editMessageReplyMarkup", {
      chat_id: q.message.chat.id, message_id: q.message.message_id, reply_markup: m.reply_markup,
    });
  }
  if (data === "as_ok") {
    if (!d.assets.length) return answer("Elige al menos 1 activo.", true);
    u.strategies = d.strategies;
    u.assets = d.assets;
    u.draft = null;
    await putUser(env, u);
    await answer("Guardado");
    return edit(env, q.message,
      `✅ *Listo, ${esc(u.name)}.*\n\nVas a recibir señales de: ${strategyNames(u.strategies)}\n` +
      `En: ${assetNames(u.assets)}\n\n${HELP}\n\n${DISCLAIMER}`);
  }
  return answer("");
}
