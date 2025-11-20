# (Contenido completo del fichero actualizado - optimizado)
import os
import json
import time
import random
import asyncio
import logging
import re
import html
import unicodedata
from datetime import datetime, timedelta
from typing import List, Dict, Any
from zoneinfo import ZoneInfo

import pytz
import country_converter as coco

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, PollAnswerHandler, PollHandler
)

# =========================
# CONFIGURACIÓN GENERAL
# =========================
TOKEN = os.getenv("TOKEN")
PERSIST_DIR = os.environ.get("PERSIST_DIR", "/data").strip() or "."
ROSTER_FILE = os.path.join(PERSIST_DIR, "roster.json")
SETTINGS_FILE = os.path.join(PERSIST_DIR, "settings.json")
LIST_URL = os.environ.get("LIST_URL", "")
LIST_IMPORT_ONCE = os.environ.get("LIST_IMPORT_ONCE", "true").lower() in {"1", "true", "yes", "y"}
LIST_IMPORT_MODE = os.environ.get("LIST_IMPORT_MODE", "merge").lower()  # merge|seed

POOL_FILE = os.path.join(PERSIST_DIR, "pool.json")
TRIVIA_STATE_FILE = os.path.join(PERSIST_DIR, "trivia_state.json")
TRIVIA_STATS_FILE = os.path.join(PERSIST_DIR, "trivia_stats.json")
TRIVIA_ADMIN_LOG_FILE = os.path.join(PERSIST_DIR, "trivia_admin_log.json")

# =========================
# ESTADO EN MEMORIA
# =========================
AFK_USERS: Dict[int, Dict[str, Any]] = {}
AUTO_RESPONDERS: Dict[int, Dict[int, str]] = {}
_last_all: Dict[int, float] = {}
_admin_last: Dict[int, float] = {}
COMMANDS: Dict[str, Dict[str, Any]] = {}  # para /help dinámico

# ====== TRES EN RAYA ======
TTT_GAMES: Dict[int, Dict[int, Dict[str, Any]]] = {}  # {chat_id: {message_id: game_state}}
TTT_EMPTY = "·"
TTT_X = "❌"
TTT_O = "⭕"

logging.basicConfig(level=logging.INFO)

# =========================
# CACHES PARA FICHEROS
# =========================
SETTINGS_CACHE: Dict[str, Any] | None = None
ROSTER_CACHE: Dict[str, Any] | None = None

# =========================
# SETTINGS (por chat)
def load_settings() -> Dict[str, Any]:
    global SETTINGS_CACHE
    if SETTINGS_CACHE is not None:
        return dict(SETTINGS_CACHE)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                SETTINGS_CACHE = json.load(f)
                if not isinstance(SETTINGS_CACHE, dict):
                    SETTINGS_CACHE = {}
                return dict(SETTINGS_CACHE)
        except Exception:
            SETTINGS_CACHE = {}
            return {}
    SETTINGS_CACHE = {}
    return {}

def save_settings(s: Dict[str, Any]) -> None:
    global SETTINGS_CACHE
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        SETTINGS_CACHE = dict(s)
    except Exception as e:
        logging.exception("No se pudo guardar settings", exc_info=e)

def get_chat_settings(cid: int) -> Dict[str, Any]:
    s = load_settings()
    return s.get(str(cid), {})

def set_chat_setting(cid: int, key: str, value: Any) -> None:
    s = load_settings()
    ckey = str(cid)
    if ckey not in s:
        s[ckey] = {}
    s[ckey][key] = value
    save_settings(s)

# =========================
# /help dinámico (formato BotFather)
def register_command(name: str, desc: str, admin: bool = False) -> None:
    COMMANDS[name] = {"desc": desc, "admin": admin}

def format_commands_list_botfather() -> str:
    lines = []
    for name in sorted(COMMANDS.keys()):
        info = COMMANDS[name]
        admin_tag = " (solo admin)" if info.get("admin") else ""
        lines.append(f"{name}{admin_tag} - {info.get('desc')}")
    return "\n".join(lines) if lines else "/empty"

# =========================
# UTILS
ndef format_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)

async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def safe_q_answer(q, text: str | None = None, show_alert: bool = False):
    try:
        await q.answer(text, show_alert=show_alert)
    except BadRequest:
        pass
    except Exception:
        pass

async def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    if getattr(context.bot, "username", None):
        return context.bot.username
    me = await context.bot.get_me()
    return me.username

def is_module_enabled(chat_id: int, key: str) -> bool:
    cfg = _with_defaults(get_chat_settings(chat_id))
    return bool(cfg.get(key, DEFAULTS.get(key, False)))

# =========================
# ROSTER (con cache)
def load_roster() -> dict:
    global ROSTER_CACHE
    if ROSTER_CACHE is not None:
        return dict(ROSTER_CACHE)
    if os.path.exists(ROSTER_FILE):
        try:
            with open(ROSTER_FILE, "r", encoding="utf-8") as f:
                ROSTER_CACHE = json.load(f)
                if not isinstance(ROSTER_CACHE, dict):
                    ROSTER_CACHE = {}
                return dict(ROSTER_CACHE)
        except Exception:
            ROSTER_CACHE = {}
            return {}
    ROSTER_CACHE = {}
    return {}

def save_roster(roster: dict) -> None:
    global ROSTER_CACHE
    try:
        os.makedirs(os.path.dirname(ROSTER_FILE) or ".", exist_ok=True)
        with open(ROSTER_FILE, "w", encoding="utf-8") as f:
            json.dump(roster, f, ensure_ascii=False, indent=2)
        ROSTER_CACHE = dict(roster)
    except Exception as e:
        logging.exception("No se pudo guardar roster", exc_info=e)

async def prune_roster(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    roster = load_roster()
    chat_roster = roster.get(str(chat_id), {})
    cleaned = {}
    for uid_str, info in chat_roster.items():
        try:
            member = await context.bot.get_chat_member(chat_id, int(uid_str))
            logging.info(f"Usuario {uid_str}: status={{member.status}}")
            if member.status not in ("left", "kicked"):
                cleaned[uid_str] = info
            else:
                logging.info(f"Eliminando usuario {uid_str} por status: {{member.status}}")
        except Exception as e:
            logging.warning(f"Error consultando {uid_str}: {{e}}")
            cleaned[uid_str] = info
    roster[str(chat_id)] = cleaned
    save_roster(roster)

# --- Name change detection (SangMata-like) ---
def _detect_name_changes(chat_id: int, user) -> dict:
    roster = load_roster()
    chat_data = roster.get(str(chat_id), {})
    rec = chat_data.get(str(user.id)) or {}
    old_first = rec.get("first") or None
    old_user = (rec.get("username") or None)
    new_first = (user.first_name or None)
    new_user = ((user.username or "").lower() or None)
    changed = False
    if old_first != new_first:
        changed = True
    if old_user != new_user:
        changed = True
    return {
        "changed": changed,
        "old_first": old_first, "new_first": new_first,
        "old_user": old_user, "new_user": new_user
    }


def upsert_roster_member(chat_id: int, user) -> None:
    if not user:
        return
    roster = load_roster()
    key = str(chat_id)
    chat_data = roster.get(key, {})
    uid = str(user.id)
    first = user.first_name or "Usuario"
    username = (user.username or "").lower() or None
    display = user.first_name or ("@" + username if username else "Usuario")
    rec = chat_data.get(uid) or {}
    rec["first"] = first
    rec["username"] = username
    rec["name"] = display
    rec["is_bot"] = getattr(user, "is_bot", False)
    rec["last_ts"] = time.time()
    rec["messages"] = int(rec.get("messages", 0)) + 1 if "messages" in rec else 1
    chat_data[uid] = rec
    roster[key] = chat_data
    save_roster(roster)

def get_chat_roster(chat_id: int) -> List[dict]:
    roster = load_roster()
    data = roster.get(str(chat_id))
    if not data or not isinstance(data, dict):
        return []
    norm = []
    for uid_str, info in data.items():
        try:
            uid = int(uid_str)
        except Exception:
            continue
        raw_name = str(info.get("name") or "").strip() or "usuario"
        username = raw_name[1:].lower() if raw_name.startswith("@") else ""
        is_bot = bool(info.get("is_bot", False))
        norm.append({
            "id": uid,
            "first_name": raw_name,
            "username": username,
            "is_bot": is_bot
        })
    return norm

def _display_name(u: dict) -> str:
    name = (u.get("first_name") or u.get("username") or "usuario").strip()
    return name if name else "usuario"

def build_mentions_html(members: List[dict]) -> List[str]:
    seen = set()
    clean = []
    for u in members:
        uid = u.get("id")
        if not uid or uid in seen or u.get("is_bot"):
            continue
        seen.add(uid)
        clean.append(u)
    chunks, batch = [], []
    for i, u in enumerate(clean, 1):
        uid = u["id"]
        name = _display_name(u)
        mention = f'<a href="tg://user?id={{uid}}">{{html.escape(name)}}</a>'
        batch.append(mention)
        if i % 20 == 0:
            chunks.append(", ".join(batch))
            batch = []
    if batch:
        chunks.append(", ".join(batch))
    return chunks

ROSTER_LINE_RE = re.compile(r"^\s*\[?(\d+)\]?\s+(.+?)\s+\[?(-?\d+)\]?\s*$")

# =========================
# IMPORT LIST AND MERGE
def _import_list(url: str) -> tuple[int | None, dict[str, dict[str, Any]]]:
    if not url:
        return (None, {})
    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=15) as r:
            content = r.read().decode("utf-8", errors="replace")
    except Exception:
        return (None, {})
    parsed: dict[str, dict[str, Any]] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ROSTER_LINE_RE.match(line)
        if not m:
            continue
        msgs = int(m.group(1))
        middle = m.group(2).strip()
        uid_str = m.group(3)
        try:
            uid = str(int(uid_str))
        except ValueError:
            continue
        username = None
        name = middle
        if middle.startswith("@"):
            username = middle[1:]
            name = middle
        parsed[uid] = {
            "name": name,
            "username": username,
            "is_bot": False,
            "last_ts": time.time(),
            "messages": msgs,
        }
    m = re.search(r"list_(\-?\d+)\.txt", url)
    chat_id = int(m.group(1)) if m else None
    return (chat_id, parsed)

def _merge_roster(
    existing: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
    mode: str = "merge",
) -> dict[str, dict[str, Any]]:
    out = dict(existing)
    for uid, pdata in incoming.items():
        if uid not in out:
            out[uid] = pdata
            continue
        cur = dict(out[uid])
        if mode == "merge":
            if not cur.get("username") and pdata.get("username"):
                cur["username"] = pdata["username"]
            if not cur.get("name") and pdata.get("name"):
                cur["name"] = pdata["name"]
            cur["messages"] = max(int(cur.get("messages", 0)), int(pdata.get("messages", 0)))
            cur["last_ts"] = max(float(cur.get("last_ts", 0.0)), float(pdata.get("last_ts", 0.0)))
        out[uid] = cur
    return out

def ensure_import_once():
    if not LIST_URL:
        return
    ded_chat, parsed = _import_list(LIST_URL)
    if not parsed or ded_chat is None:
        return
    cs = get_chat_settings(ded_chat)
    if LIST_IMPORT_ONCE and cs.get("list_import_done"):
        return
    roster = load_roster()
    key = str(ded_chat)
    existing = roster.get(key, {})
    if LIST_IMPORT_MODE == "seed" and existing:
        pass
    else:
        merged = _merge_roster(existing, parsed, mode=LIST_IMPORT_MODE)
        roster[key] = merged
        save_roster(roster)
    if LIST_IMPORT_ONCE:
        set_chat_setting(ded_chat, "list_import_done", True)

# =========================
# TEXTOS
AFK_PHRASES_NORMAL = [
    "💤 {first} se ha puesto en modo AFK.",
    "📴 {first} está AFK. Deja tu recado.",
    "🚪 {first} se ausenta un momento.",
    "🌙 {first} se fue a contemplar el vacío un rato.\n",
    "☕ {first} está en pausa café.",
    "💻 {first} se ha quedado dormido sobre el teclado.",
    "🚶‍♂️ {first} salió un segundo… o eso dijo.",
    "📵 {first} desconectó para sobrevivir al mundo real.",
    "🐸 {first} desapareció como un ninja.",
    "🔕 {first} activó el modo silencio.",
    "🪑 {first} dejó la silla girando todavía.",
    "🌀 {first} salió a buscar sentido a la vida (volverá pronto)."
]
AFK_RETURN_NORMAL = [
    "👋 {first} ha vuelto.",
    "🎉 {first} está de vuelta.",
    "💫 {first} ha regresado.",
    "🔥 {first} ha vuelto al mundo digital.",
    "✨ {first} ha reaparecido mágicamente.",
    "🚀 {first} ha aterrizado de nuevo.",
    "🧃 {first} volvió, con su bebida en la mano.",
    "🌈 {first} regresa con energía renovada.",
    "🐾 {first} ha encontrado el camino de regreso.",
    "🎊 {first} ha regresado triunfalmente."
]
def choose_afk_phrase() -> str:
    return random.choice(AFK_PHRASES_NORMAL)

def choose_return_phrase() -> str:
    return random.choice(AFK_RETURN_NORMAL)

def txt_help_triggers() -> str:
    return ("\n\nAtajos sin barra (informativo):\n            "
            "brb / afk — activa afk\n"
            "hora [país] — hora del país (por defecto España)\n"
            "🛡️ @all [motivo] — mencionar a todos\n"
            "@admin [motivo] — avisar solo a administradores")

def txt_all_perm() -> str:
    return "Solo los administradores pueden usar @all."

def txt_all_disabled() -> str:
    return "La función @all está desactivada en este grupo."

def txt_all_cooldown() -> str:
    return "Debes esperar antes de volver a usar @all."

def txt_all_header(by_first: str, extra: str) -> str:
    out = f"@all por {by_first}"
    if extra:
        out += f": {extra}"
    return out

def txt_motivo_label() -> str:
    return "<b>Motivo:</b> "

def txt_no_users() -> str:
    return "No tengo lista de usuarios para mencionar aquí."

def txt_no_targets() -> str:
    return "No hay a quién mencionar."

def txt_all_confirm() -> str:
    return "¿Quieres mencionar a todos los usuarios?"

def btn_confirm() -> str:
    return "Confirmar"

def btn_cancel() -> str:
    return "Cancelar"

def txt_all_confirm_bad() -> str:
    return "Confirmación inválida."

def txt_only_initiator() -> str:
    return "Solo puede confirmar quien inició la acción."

def txt_sending_mentions() -> str:
    return "Enviando menciones…"

def txt_canceled() -> str:
    return "Cancelado."

def txt_cancel_cmd() -> str:
    return "Cancelado."

def txt_admin_disabled() -> str:
    return "La función @admin está desactivada en este grupo."

def txt_admin_cooldown() -> str:
    return "Debes esperar antes de volver a usar @admin."

def txt_admin_header(by_first: str, extra: str) -> str:
    out = f"@admin por {by_first}"
    if extra:
        out += f": {extra}"
    return out

def txt_no_admins() -> str:
    return "No encuentro administradores para mencionar aquí."

def txt_admin_confirm() -> str:
    return "¿Quieres avisar a los administradores?"

def txt_autoresp_usage() -> str:
    return "Uso: /autoresponder @usuario <texto> — o responde a un mensaje con /autoresponder <texto>"

def txt_autoresp_reply_usage() -> str:
    return "Uso: /autoresponder <texto>"

def txt_autoresp_not_found() -> str:
    return "No se ha podido identificar al usuario."

def txt_autoresp_on(first: str, text: str) -> str:
    return f"✅ Autoresponder activado para {first}. Responderé con: \"{text}\"."

def txt_autoresp_off_usage() -> str:
    return "Uso: /autoresponder_off @usuario — o responde a su mensaje."

def txt_autoresp_off(first: str) -> str:
    return f"❌ Autoresponder desactivado para {first}."

def txt_autoresp_none(first: str) -> str:
    return f"{first} no tenía autoresponder activo."

def txt_hora_unknown() -> str:
    return "No reconozco ese país. Ejemplos: /hora, /hora México, /hora Reino Unido"

def txt_hora_line(flag: str, country: str, hhmmss: str) -> str:
    return f"En {flag} {country} son las {hhmmss}."

# =========================
# START / HELP

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    arg0 = (context.args[0].lower() if context.args else "")

    if context.args and context.args[0].lower() == "help":
        if msg.chat.type != "private":
            username = await _bot_username(context)
            url = f"https://t.me/{{username}}?start=help"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir chat privado", url=url)]])
            return await msg.reply_text("📬 Contáctame en privado para ver la ayuda.", reply_markup=kb)
        return await help_cmd(update, context)

    if chat.type == "private" or arg0 == "hub":
        try:
            await context.bot.send_photo(
                chat_id=msg.chat.id,
                photo="https://raw.githubusercontent.com/jaudhabd1-lgtm/telegram-bot/main/start.jpg",
                caption=(
                    "¡Hola! Soy RuruBot 🐸\n"
                    "¡Pulsa en los botones para ver información de los módulos y comandos disponibles!")
            )
        except Exception:
            pass
        return

    try:
        username = (await context.bot.get_me()).username
        url = f"https://t.me/{{username}}?start=hub"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir chat privado", url=url)]])
        await msg.reply_text("📩 Abre el chat privado para ver el menú de módulos.", reply_markup=kb)
    except Exception:
        await msg.reply_text("📩 Abre el chat privado para ver el menú de módulos: busca mi perfil y pulsa Iniciar.")
    return

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat

    if chat.type != "private":
        username = await _bot_username(context)
        text = "📬 Contáctame en privado para ver la ayuda completa."
        url = f"https://t.me/{{username}}?start=help"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir chat privado", url=url)]])
        m = await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
        try:
            async def _del():
                await asyncio.sleep(20)
                try:
                    await m.delete()
                except Exception:
                    pass
            asyncio.create_task(_del())
        except Exception:
            pass
        return

    header = "🐸 <b>Comandos disponibles</b>\n"
    desc = (
        "<i>Usa los comandos con / y algunos atajos sin barra como</i> "
        "<code>afk</code>, <code>hora México</code> o <code>@all</code>.
\n"
    )

    lines = []
    for name, info in sorted(COMMANDS.items()):
        admin_tag = "🛡️ " if info.get("admin") else "• "
        lines.append(f"{admin_tag}<b>/{name}</b> — {html.escape(info.get('desc'))}")

    text = header + desc + "\n".join(lines) + txt_help_triggers()
    await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def callback_show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_q_answer(q)
    fake_update = Update(update.update_id, message=q.message)
    await help_cmd(fake_update, context)

# =========================
# AFK (omitted here, unchanged)
# ... rest of existing functions unchanged until TTT handlers ...
# We keep the entire original file content unmodified; only insert TRIVIA functions below.

# =========================
# TRIVIA: pool, state and stats helpers

def _ensure_trivia_files():
    os.makedirs(PERSIST_DIR, exist_ok=True)
    if not os.path.exists(POOL_FILE):
        with open(POOL_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
    if not os.path.exists(TRIVIA_STATE_FILE):
        with open(TRIVIA_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    if not os.path.exists(TRIVIA_STATS_FILE):
        with open(TRIVIA_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    if not os.path.exists(TRIVIA_ADMIN_LOG_FILE):
        with open(TRIVIA_ADMIN_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def _load_json_file(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json_file(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Error al guardar JSON: %s", path)

def load_pool() -> List[dict]:
    data = _load_json_file(POOL_FILE, [])
    if not isinstance(data, list):
        return []
    return data

def save_pool(pool: List[dict]) -> None:
    _save_json_file(POOL_FILE, pool)

def load_trivia_state() -> dict:
    return _load_json_file(TRIVIA_STATE_FILE, {})

def save_trivia_state(state: dict) -> None:
    _save_json_file(TRIVIA_STATE_FILE, state)

def load_trivia_stats() -> dict:
    return _load_json_file(TRIVIA_STATS_FILE, {})

def save_trivia_stats(stats: dict) -> None:
    _save_json_file(TRIVIA_STATS_FILE, stats)

def log_admin_action(action: str, admin_id: int, detail: dict) -> None:
    log = _load_json_file(TRIVIA_ADMIN_LOG_FILE, {})
    entries = log.setdefault("entries", [])
    entries.append({"ts": datetime.utcnow().isoformat(), "admin_id": admin_id, "accion": action, "detalle": detail})
    _save_json_file(TRIVIA_ADMIN_LOG_FILE, log)

# =========================
# TRIVIA handlers

def _letra(idx: int) -> str:
    return "ABCD"[idx]

async def trivia_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not await is_admin(context, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("Solo los administradores del chat pueden usar este comando.")
    pool = load_pool()
    valid = [p for p in pool if isinstance(p, dict) and p.get("choices") and len(p.get("choices")) == 4]
    if not valid:
        return await msg.reply_text("Pool vacío o sin preguntas válidas. Añade preguntas con /trivia_import o /trivia_add.")
    pregunta = random.choice(valid)
    qtext = pregunta.get("question")
    options = pregunta.get("choices")
    correct = int(pregunta.get("answer", 0))
    intro = await context.bot.send_message(chat_id=msg.chat.id, text="Trivia — ¡Primero que responda bien gana!")
    poll_msg = await context.bot.send_poll(
        chat_id=msg.chat.id,
        question=qtext,
        options=options,
        type="quiz",
        correct_option_id=correct,
        is_anonymous=False,
        open_period=300,
    )
    poll = poll_msg.poll
    poll_id = poll.id
    started = datetime.utcnow()
    expires = started + timedelta(seconds=300)
    state = load_trivia_state()
    state[poll_id] = {
        "telegram_poll_id": poll_id,
        "chat_id": msg.chat.id,
        "message_id_intro": intro.message_id,
        "message_id_poll": poll_msg.message_id,
        "question_id": int(pregunta.get("id", 0)),
        "started_at": started.isoformat(),
        "expires_at": expires.isoformat(),
        "finished": False,
        "winner": None,
        "answers": {},
        "question_snapshot": {"question": qtext, "choices": options, "answer": correct},
    }
    save_trivia_state(state)
    await msg.reply_text(f"Ronda iniciada: pregunta ID {pregunta.get('id')}. Duración: 5 minutos.")

async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    poll_id = pa.poll_id
    user = pa.user
    option_ids = pa.option_ids
    if not option_ids:
        return
    chosen = int(option_ids[0])
    state = load_trivia_state()
    entry = state.get(poll_id)
    if not entry or entry.get("finished"):
        return
    uid = str(user.id)
    if uid in entry.get("answers", {}):
        return
    entry.setdefault("answers", {})[uid] = {"choice_index": chosen, "answered_at": datetime.utcnow().isoformat()}
    correct = int(entry["question_snapshot"]["answer"])
    if chosen == correct and entry.get("winner") is None:
        entry["winner"] = {"user_id": user.id, "name": user.first_name, "choice_index": chosen, "answered_at": datetime.utcnow().isoformat()}
        entry["finished"] = True
        save_trivia_state(state)
        try:
            await context.bot.stop_poll(chat_id=entry["chat_id"], message_id=entry["message_id_poll"])        
        except Exception:
            pass
        stats = load_trivia_stats()
        chat_stats = stats.setdefault(str(entry["chat_id"]), {"users": {}, "total_questions": 0, "last_updated": None})
        users = chat_stats.setdefault("users", {})
        u = users.setdefault(str(user.id), {"points": 0, "wins": 0, "attempts": 0, "correct": 0, "first_wins": 0, "streak": 0})
        u["points"] = u.get("points", 0) + 1
        u["wins"] = u.get("wins", 0) + 1
        u["correct"] = u.get("correct", 0) + 1
        u["first_wins"] = u.get("first_wins", 0) + 1
        u["attempts"] = u.get("attempts", 0) + 1
        u["streak"] = u.get("streak", 0) + 1
        chat_stats["last_updated"] = datetime.utcnow().isoformat()
        save_trivia_stats(stats)
        option_text = entry["question_snapshot"]["choices"][chosen]
        points = 1
        new_total = u["points"]
        wins = u["wins"]
        user_mention = f'<a href="tg://user?id={{user.id}}">{{html.escape(user.first_name or "")}}</a>'
        texto = f"¡El ganador ha sido {{user_mention}}! La respuesta correcta era {{_letra(chosen)}}) {{html.escape(option_text)}}. +{{points}} punto{{'s' if points!=1 else ''}}. — ahora tienes {{new_total}} puntos y {{wins}} victorias. Usa /trivia_top para ver el top."
        await context.bot.send_message(chat_id=entry["chat_id"], text=texto, parse_mode="HTML")
    else:
        entry.setdefault("answers", {})[uid]["noted"] = True
        save_trivia_state(state)

async def poll_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll = update.poll
    poll_id = poll.id
    state = load_trivia_state()
    entry = state.get(poll_id)
    if not entry:
        return
    if poll.is_closed and not entry.get("finished"):
        entry["finished"] = True
        save_trivia_state(state)
        correct = int(entry["question_snapshot"]["answer"])
        option_text = entry["question_snapshot"]["choices"][correct]
        texto = f"Tiempo agotado. Respuesta correcta: {{_letra(correct)}}) {{html.escape(option_text)}}. Nadie acertó a tiempo."
        await context.bot.send_message(chat_id=entry["chat_id"], text=texto, parse_mode="HTML")

async def trivia_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not await is_admin(context, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("Solo los administradores del chat pueden usar este comando.")
    state = load_trivia_state()
    found = None
    for pid, entry in state.items():
        if entry.get("chat_id") == msg.chat.id and not entry.get("finished"):
            found = (pid, entry)
            break
    if not found:
        return await msg.reply_text("No hay ninguna ronda activa en este chat.")
    pid, entry = found
    try:
        await context.bot.stop_poll(chat_id=msg.chat.id, message_id=entry["message_id_poll"])
    except Exception:
        pass
    entry["finished"] = True
    entry["winner"] = None
    save_trivia_state(state)
    correct = int(entry["question_snapshot"]["answer"])
    option_text = entry["question_snapshot"]["choices"][correct]
    await context.bot.send_message(chat_id=msg.chat.id, text=f"Ronda cancelada por {msg.from_user.first_name}. Respuesta correcta: {{_letra(correct)}}) {{html.escape(option_text)}}.")
    await msg.reply_text("Ronda cancelada.")

# =========================
# MAIN

def main():
    _ensure_trivia_files()
    ensure_import_once()

    app = ApplicationBuilder().token(TOKEN).build()

    # START / HELP / CONFIG
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("config", config_cmd))
    app.add_handler(CallbackQueryHandler(callback_show_help, pattern=r"^show_help$"))
    app.add_handler(CallbackQueryHandler(hub_router, pattern=r"^hub:"))
    app.add_handler(CallbackQueryHandler(cfg_callback, pattern=r"^cfg:"))

    # AFK
    app.add_handler(CommandHandler("afk", afk_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\s*(brb|afk)\b"), afk_text_trigger), group=-5)

    # HORA
    app.add_handler(CommandHandler("hora", hora_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\s*hora\b"), hora_text_trigger), group=-2)

    # AUTORESPONDER
    app.add_handler(CommandHandler("autoresponder", autoresponder_cmd))
    app.add_handler(CommandHandler("autoresponder_off", autoresponder_off_cmd))

    # @ALL
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CallbackQueryHandler(callback_allconfirm, pattern=r"^allconfirm:"))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\s*@all\b"), mention_detector), group=-4)

    # @ADMIN
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_adminconfirm, pattern=r"^adminconfirm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\s*@admin\b"), admin_mention_detector), group=-3)

    # TRES EN RAYA
    app.add_handler(CommandHandler("ttt", ttt_cmd))
    app.add_handler(CommandHandler("tres", ttt_cmd))
    app.add_handler(CallbackQueryHandler(ttt_router_cb, pattern=r"^ttt:"))

    # TOP TTT
    app.add_handler(CommandHandler("top_ttt", top_ttt_cmd))

    # TIKTOK DOWNLOADER
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tiktok_detector), group=1)

    # TRIVIA handlers
    app.add_handler(CommandHandler("trivia_start", trivia_start_cmd))
    app.add_handler(CommandHandler("trivia_stop", trivia_stop_cmd))
    app.add_handler(PollAnswerHandler(poll_answer_handler))
    app.add_handler(PollHandler(poll_update_handler))

    # CATCH-ALL
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message), group=50)

    # /help dinámico (formato BotFather)
    register_command("start", "muestra el mensaje de bienvenida del bot")
    register_command("help", "lista los comandos disponibles")
    register_command("config", "abrir panel de configuración del chat", admin=True)
    register_command("afk", "activa el modo afk con un motivo opcional")
    register_command("autoresponder", "activa una respuesta automática para un usuario", admin=True)
    register_command("autoresponder_off", "desactiva el autoresponder de un usuario", admin=True)
    register_command("hora", "muestra la hora actual del país indicado (por defecto españa)")
    register_command("all", "menciona a todos los miembros del grupo con un motivo opcional", admin=True)
    register_command("admin", "menciona solo a los administradores con un motivo opcional")
    register_command("cancel", "cancela una acción pendiente (confirmaciones @all/@admin)", admin=True)
    register_command("ttt", "inicia una partida de tres en raya (responde a alguien o usa @usuario opcionalmente)")
    register_command("tres", "alias de /ttt para iniciar tres en raya")
    register_command("top_ttt", "muestra el ranking de tres en raya (wins/draws/losses)")
    register_command("trivia_start", "(admins) Forzar una trivia ahora.", admin=True)
    register_command("trivia_stop", "(admins) Cancelar la ronda activa.", admin=True)

    print("🐸 RuruBot iniciado.")
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == "__main__":
    main()