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
from datetime import datetime
from typing import List, Dict, Any
from zoneinfo import ZoneInfo

import pytz
import country_converter as coco

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
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
SETTINGS_CACHE: Dict[str, Any] | None = None
ROSTER_CACHE: Dict[str, Any] | None = None

# =========================
# SETTINGS (por chat)
def load_settings() -> Dict[str, Any]:
    global SETTINGS_CACHE
    if SETTINGS_CACHE is not None:
        # return shallow copy to avoid accidental external mutation
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
def format_duration(seconds: float) -> str:
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
    """
    Elimina del roster los usuarios que ya no están en el grupo.
    Si hay error al consultar el estado de un usuario, lo mantiene en el roster.
    """
    roster = load_roster()
    chat_roster = roster.get(str(chat_id), {})
    cleaned = {}
    for uid_str, info in chat_roster.items():
        try:
            member = await context.bot.get_chat_member(chat_id, int(uid_str))
            logging.info(f"Usuario {uid_str}: status={member.status}")
            if member.status not in ("left", "kicked"):
                cleaned[uid_str] = info
            else:
                logging.info(f"Eliminando usuario {uid_str} por status: {member.status}")
        except Exception as e:
            logging.warning(f"Error consultando {uid_str}: {e}")
            # Si hay error, mantenlo en el roster por seguridad.
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
    display = user.first_name or (("@" + username) if username else "Usuario")
    rec = chat_data.get(uid) or {}
    rec["first"] = first
    rec["username"] = username  # may be None
    rec["name"] = display
    rec["is_bot"] = getattr(user, "is_bot", False)  # <-- Cambiado: almacena valor real de Telegram
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
        is_bot = bool(info.get("is_bot", False))  # <-- Cambiado: lee valor fijo, no heurístico
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
        mention = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
        batch.append(mention)
        if i % 20 == 0:
            chunks.append(", ".join(batch))
            batch = []
    if batch:
        chunks.append(", ".join(batch))
    return chunks

ROSTER_LINE_RE = re.compile(r"^\s*\[?(\d+)\]?\s+(.+?)\s+\[?(-?\d+)\]?\s*$")


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
    "🌙 {first} se fue a contemplar el vacío un rato.",
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

    # Deep link: t.me/<bot>?start=help → muestra la ayuda directamente
    if context.args and context.args[0].lower() == "help":
        if msg.chat.type != ChatType.PRIVATE:
            username = await _bot_username(context)
            url = f"https://t.me/{username}?start=help"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir chat privado", url=url)]])
            return await msg.reply_text("📬 Contáctame en privado para ver la ayuda.", reply_markup=kb)
        return await help_cmd(update, context)

    if chat.type == ChatType.PRIVATE or arg0 == "hub":
        try:
            await context.bot.send_photo(
                chat_id=msg.chat.id,
                photo="https://raw.githubusercontent.com/jaudhabd1-lgtm/telegram-bot/main/start.jpg",
                caption=(
                    "¡Hola! Soy RuruBot 🐸\n"
                    "¡Pulsa en los botones para ver información de los módulos y comandos disponibles!"),
                reply_markup=build_hub_keyboard()
            )
        except Exception:
            pass
        return

    try:
        username = (await context.bot.get_me()).username
        url = f"https://t.me/{username}?start=hub"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir chat privado", url=url)]])
        await msg.reply_text("📩 Abre el chat privado para ver el menú de módulos.", reply_markup=kb)
    except Exception:
        await msg.reply_text("📩 Abre el chat privado para ver el menú de módulos: busca mi perfil y pulsa Iniciar.")
    return


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat

    # En grupos: redirigir a privado con botón y autodestrucción
    if chat.type != ChatType.PRIVATE:
        username = await _bot_username(context)
        text = "📬 Contáctame en privado para ver la ayuda completa."
        url = f"https://t.me/{username}?start=help"
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

    # En privado: ayuda completa (formato elegante)
    header = "🐸 <b>Comandos disponibles</b>\n"
    desc = (
        "<i>Usa los comandos con / y algunos atajos sin barra como</i> "
        "<code>afk</code>, <code>hora México</code> o <code>@all</code>.\n\n"
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
# AFK
async def afk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    user = msg.from_user

    # Respect module toggle
    if not is_module_enabled(chat.id, "afk_enabled"):
        return await msg.reply_text("El módulo AFK está desactivado en este chat.")

    reason = " ".join(context.args) if context.args else None
    AFK_USERS[user.id] = {"since": time.time(), "reason": reason, "username": (user.username or "").lower(), "first_name": user.first_name}
    phrase = choose_afk_phrase().format(first=user.first_name)
    if reason:
        phrase += " Motivo: " + reason
    await msg.reply_text(phrase)

async def afk_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # Respect module toggle
    if not is_module_enabled(msg.chat.id, "afk_enabled"):
        return

    t = msg.text.strip()
    m = re.match(r"(?is)^\s*(brb|afk)\b[^\S\r\n]*(.*)$", t)
    if not m:
        return
    reason = m.group(2).strip()
    context.args = reason.split() if reason else []
    context.user_data["afk_skip_message_id"] = msg.message_id
    await afk_cmd(update, context)

async def notify_if_mentioning_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Respect module toggle
    if not is_module_enabled(msg.chat.id, "afk_enabled"):
        return

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        if target.id in AFK_USERS:
            data = AFK_USERS[target.id]
            since = data.get("since")
            reason = data.get("reason")
            dur = format_duration(time.time() - since)
            txt = f"💤 {target.first_name} está AFK desde {dur}."
            if reason:
                txt += " Motivo: " + reason
            await msg.reply_text(txt)
    if not msg.entities:
        return
    afk_by_username = {(info.get("username") or ""): uid for uid, info in AFK_USERS.items() if info.get("username")}
    for ent in msg.entities:
        if ent.type == "mention":
            username = msg.text[ent.offset + 1:ent.offset + ent.length].lower()
            uid = afk_by_username.get(username)
            if uid:
                info = AFK_USERS[uid]
                first = info.get("first_name")
                since = info.get("since")
                reason = info.get("reason")
                dur = format_duration(time.time() - since)
                txt = f"💤 {first} está AFK desde {dur}."
                if reason:
                    txt += " Motivo: " + reason
                await msg.reply_text(txt)


# =========================
# AUTORESPONDER
async def autoresponder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # SOLO ADMIN
    if not await is_admin(context, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("Este comando es solo para administradores.")
    chat = msg.chat

    target_user = None
    response_text = None

    if msg.reply_to_message:
        target_user = msg.reply_to_message.from_user
        response_text = " ".join(context.args).strip()
        if not response_text:
            await msg.reply_text(txt_autoresp_reply_usage())
            return
    else:
        if len(context.args) < 2:
            await msg.reply_text(txt_autoresp_usage())
            return
        mention = context.args[0]
        response_text = " ".join(context.args[1:]).strip()
        if not mention.startswith("@"):
            await msg.reply_text("Debes indicar un @usuario válido o usar el comando en respuesta a su mensaje.")
            return
        username = mention[1:].lower()
        roster = load_roster().get(str(chat.id), {})
        uid = None
        for uid_str, info in roster.items():
            name = str(info.get("name") or "").strip()
            if name.startswith("@") and name[1:].lower() == username:
                uid = int(uid_str)
                break
        if uid is None:
            await msg.reply_text(txt_autoresp_not_found())
            return
        member = await context.bot.get_chat_member(chat.id, uid)
        target_user = member.user

    if chat.id not in AUTO_RESPONDERS:
        AUTO_RESPONDERS[chat.id] = {}
    AUTO_RESPONDERS[chat.id][target_user.id] = response_text
    await msg.reply_text(txt_autoresp_on(target_user.first_name, response_text))

async def autoresponder_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # SOLO ADMIN
    if not await is_admin(context, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("Este comando es solo para administradores.")
    chat = msg.chat
    target_user = None

    if msg.reply_to_message:
        target_user = msg.reply_to_message.from_user
    elif context.args and context.args[0].startswith("@"):
        username = context.args[0][1:].lower()
        roster = load_roster().get(str(chat.id), {})
        uid = None
        for uid_str, info in roster.items():
            name = str(info.get("name") or "").strip()
            if name.startswith("@") and name[1:].lower() == username:
                uid = int(uid_str)
                break
        if uid is None:
            return await msg.reply_text(txt_autoresp_not_found())
        member = await context.bot.get_chat_member(chat.id, uid)
        target_user = member.user

    if not target_user:
        await msg.reply_text(txt_autoresp_off_usage())
        return

    if chat.id in AUTO_RESPONDERS and target_user.id in AUTO_RESPONDERS[chat.id]:
        del AUTO_RESPONDERS[chat.id][target_user.id]
        await msg.reply_text(txt_autoresp_off(target_user.first_name))
    else:
        await msg.reply_text(txt_autoresp_none(target_user.first_name))


# =========================
# HORA
_cc = coco.CountryConverter()
PRIMARY_TZ_BY_ISO2 = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "BR": "America/Sao_Paulo",
    "AU": "Australia/Sydney",
    "RU": "Europe/Moscow",
    "MX": "America/Mexico_City",
    "CN": "Asia/Shanghai",
}
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).strip()

def flag_emoji(cc: str) -> str:
    cc = cc.upper()
    if len(cc) != 2 or not cc.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in cc)

def resolve_country_to_iso2_and_name(q: str | None) -> tuple[str, str] | None:
    if not q:
        return ("ES", "España")
    q = _strip_accents(q).lower()
    for junk in (" de ", " del ", " la ", " el "):
        q = q.replace(junk, " ")
    q = " ".join(q.split())
    iso2 = _cc.convert(names=q, to="ISO2", not_found=None)
    if not iso2 or iso2 == "not found":
        return None
    pretty = _cc.convert(names=iso2, src="ISO2", to="name_short")
    if not pretty or pretty == "not found":
        pretty = iso2
    return (iso2, pretty)

def pick_timezone_for_country(iso2: str) -> str:
    iso2 = iso2.upper()
    if iso2 in PRIMARY_TZ_BY_ISO2:
        return PRIMARY_TZ_BY_ISO2[iso2]
    tzs = pytz.country_timezones.get(iso2)
    if not tzs:
        return "Europe/Madrid"
    return tzs[0]

def format_time_in_tz(tz: str) -> str:
    now = datetime.now(ZoneInfo(tz))
    return now.strftime("%H:%M:%S")

async def hora_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    query = " ".join(context.args).strip() if context.args else None
    resolved = resolve_country_to_iso2_and_name(query)
    if not resolved:
        return await msg.reply_text(txt_hora_unknown())
    iso2, country_name = resolved
    tz = pick_timezone_for_country(iso2)
    flag = flag_emoji(iso2)
    hhmmss = format_time_in_tz(tz)
    await msg.reply_text(txt_hora_line(flag, country_name, hhmmss))

async def hora_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    m = re.match(r"(?is)^\s*hora\b(.*)$", msg.text.strip())
    if not m:
        return
    query = m.group(1).strip() or None
    resolved = resolve_country_to_iso2_and_name(query)
    if not resolved:
        return await msg.reply_text(txt_hora_unknown())
    iso2, country_name = resolved
    tz = pick_timezone_for_country(iso2)
    flag = flag_emoji(iso2)
    hhmmss = format_time_in_tz(tz)
    await msg.reply_text(txt_hora_line(flag, country_name, hhmmss))


# =========================
# @ALL
async def _check_all_permissions(context, chat_id: int, user_id: int) -> tuple[bool, str]:
    if not await is_admin(context, chat_id, user_id):
        return False, txt_all_perm()
    cfg = get_chat_settings(chat_id)
    if not cfg.get("all_enabled", True):
        return False, txt_all_disabled()
    cd = cfg.get("all_cooldown_sec", 60)
    if _last_all.get(chat_id) and time.time() - _last_all[chat_id] < cd:
        return False, txt_all_cooldown()
    return True, ""

async def execute_all(chat, context: ContextTypes.DEFAULT_TYPE, extra: str, by_user):
    await prune_roster(chat.id, context)
    members = get_chat_roster(chat.id)
    if not members:
        await context.bot.send_message(chat_id=chat.id, text=txt_no_users())
        return
    parts = build_mentions_html(members)
    if not parts:
        await context.bot.send_message(chat_id=chat.id, text=txt_no_targets())
        return

    header = txt_all_header(by_user.first_name, extra)
    try:
        await context.bot.send_message(chat_id=chat.id, text=header)
    except Exception as e:
        logging.exception("Fallo cabecera @all", exc_info=e)

    motivo_html = ("\n\n" + txt_motivo_label() + html.escape(extra)) if extra else ""
    for block in parts:
        try:
            body = block + motivo_html
            await context.bot.send_message(chat_id=chat.id, text=body, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.3)
        except Exception:
            logging.exception("Fallo bloque @all")
    _last_all[chat.id] = time.time()

async def confirm_all(chat_id: int, context: ContextTypes.DEFAULT_TYPE, extra: str, initiator_id: int):
    data_yes = f"allconfirm:{chat_id}:yes:{initiator_id}"
    data_no = f"allconfirm:{chat_id}:no:{initiator_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_confirm(), callback_data=data_yes),
         InlineKeyboardButton(btn_cancel(), callback_data=data_no)]
    ])

async def callback_allconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    await safe_q_answer(q)
    try:
        _, cid, action, initiator = q.data.split(":")
        cid = int(cid)
        initiator = int(initiator)
    except Exception:
        return await q.edit_message_text(txt_all_confirm_bad())
    if q.from_user.id != initiator:
        return await q.reply_text(txt_only_initiator())
    if action == "yes":
        extra = context.user_data.get("pending_all", "")
        await q.edit_message_text(txt_sending_mentions())
        chat = await context.bot.get_chat(cid)
        await execute_all(chat, context, extra, q.from_user)
        context.user_data.pop("pending_all", None)
    else:
        context.user_data.pop("pending_all", None)
        await q.edit_message_text(txt_canceled())

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # SOLO ADMIN
    if not await is_admin(context, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("Este comando es solo para administradores.")
    context.user_data.pop("pending_all", None)
    context.user_data.pop("pending_admin", None)
    await msg.reply_text(txt_cancel_cmd())

async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    text = msg.text
    extra = text.split(" ", 1)[1] if " " in text else ""
    ok, why = await _check_all_permissions(context, chat.id, user.id)
    if not ok:
        return await msg.reply_text(why)
    cfg = get_chat_settings(chat.id)
    if cfg.get("all_confirm", True):
        kb = await confirm_all(chat.id, context, extra, user.id)
        await msg.reply_text(txt_all_confirm(), reply_markup=kb)
        context.user_data["pending_all"] = extra
        return
    await execute_all(chat, context, extra, user)

async def mention_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or (msg.from_user and msg.from_user.is_bot):
        return
    t = msg.text.strip()
    if not t.lower().startswith("@all"):
        return
    chat = msg.chat
    user = msg.from_user
    extra = t[4:].lstrip()
    ok, why = await _check_all_permissions(context, chat.id, user.id)
    if not ok:
        return await msg.reply_text(why)
    cfg = get_chat_settings(chat.id)
    if cfg.get("all_confirm", True):
        kb = await confirm_all(chat.id, context, extra, user.id)
        await msg.reply_text(txt_all_confirm(), reply_markup=kb)
        context.user_data["pending_all"] = extra
        return
    await execute_all(chat, context, extra, user)


# =========================
# @ADMIN
async def _check_admin_ping_permissions(context, chat_id: int) -> tuple[bool, str]:
    cfg = get_chat_settings(chat_id)
    if not cfg.get("admin_enabled", True):
        return False, txt_admin_disabled()
    cd = cfg.get("admin_cooldown_sec", 60)
    if _admin_last.get(chat_id) and time.time() - _admin_last[chat_id] < cd:
        return False, txt_admin_cooldown()
    return True, ""

async def _get_admin_members(chat, context: ContextTypes.DEFAULT_TYPE) -> List[dict]:
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except Exception:
        return []
    out, seen = [], set()
    for cm in admins:
        u = cm.user
        if not u or u.is_bot:
            continue
        if u.id in seen:
            continue
        seen.add(u.id)
        out.append({"id": u.id, "first_name": u.first_name or (u.username or "Admin")})
    return out

def _build_mentions_html_from_basic(members: List[dict]) -> List[str]:
    chunks, batch = [], []
    for i, u in enumerate(members, 1):
        uid = u["id"]
        name = u.get("first_name") or "usuario"
        mention = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
        batch.append(mention)
        if i % 20 == 0:
            chunks.append(", ".join(batch))
            batch = []
    if batch:
        chunks.append(", ".join(batch))
    return chunks

async def execute_admin(chat, context: ContextTypes.DEFAULT_TYPE, extra: str, by_user):
    await prune_roster(chat.id, context)
    admins = await _get_admin_members(chat, context)
    if not admins:
        return await context.bot.send_message(chat_id=chat.id, text=txt_no_admins())
    parts = _build_mentions_html_from_basic(admins)
    header = txt_admin_header(by_user.first_name, extra)
    try:
        await context.bot.send_message(chat_id=chat.id, text=header)
    except Exception:
        logging.exception("Fallo cabecera @admin")
    motivo_html = ("\n\n" + txt_motivo_label() + html.escape(extra)) if extra else ""
    for block in parts:
        try:
            body = block + motivo_html
            await context.bot.send_message(chat_id=chat.id, text=body, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.3)
        except Exception:
            logging.exception("Fallo bloque @admin")
    _admin_last[chat.id] = time.time()

async def confirm_admin(chat_id: int, context: ContextTypes.DEFAULT_TYPE, extra: str, initiator_id: int):
    data_yes = f"adminconfirm:{chat_id}:yes:{initiator_id}"
    data_no = f"adminconfirm:{chat_id}:no:{initiator_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_confirm(), callback_data=data_yes),
         InlineKeyboardButton(btn_cancel(), callback_data=data_no)]
    ])

async def callback_adminconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    await safe_q_answer(q)
    try:
        _, cid, action, initiator = q.data.split(":")
        cid = int(cid)
        initiator = int(initiator)
    except Exception:
        return await q.edit_message_text(txt_all_confirm_bad())
    if q.from_user.id != initiator:
        return await q.reply_text(txt_only_initiator())
    if action == "yes":
        extra = context.user_data.get("pending_admin", "")
        await q.edit_message_text(txt_calling_admins())
        chat = await context.bot.get_chat(cid)
        await execute_admin(chat, context, extra, q.from_user)
        context.user_data.pop("pending_admin", None)
    else:
        context.user_data.pop("pending_admin", None)
        await q.edit_message_text(txt_canceled())

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    extra = msg.text.split(" ", 1)[1] if " " in msg.text else ""
    ok, why = await _check_admin_ping_permissions(context, chat.id)
    if not ok:
        return await msg.reply_text(why)
    cfg = get_chat_settings(chat.id)
    if cfg.get("admin_confirm", True):
        kb = await confirm_admin(chat.id, context, extra, user.id)
        await msg.reply_text(txt_admin_confirm(), reply_markup=kb)
        context.user_data["pending_admin"] = extra
        return
    await execute_admin(chat, context, extra, user)

async def admin_mention_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or (msg.from_user and msg.from_user.is_bot):
        return
    t = msg.text.strip()
    if not t.lower().startswith("@admin"):
        return
    chat = msg.chat
    user = msg.from_user
    extra = t[6:].lstrip()
    ok, why = await _check_admin_ping_permissions(context, chat.id)
    if not ok:
        return await msg.reply_text(why)
    cfg = get_chat_settings(chat.id)
    if cfg.get("admin_confirm", True):
        kb = await confirm_admin(chat.id, context, extra, user.id)
        await msg.reply_text(txt_admin_confirm(), reply_markup=kb)
        context.user_data["pending_admin"] = extra
        return
    await execute_admin(chat, context, extra, user)


# =========================
# ESTADÍSTICAS TRES EN RAYA
def _ttt_stats_load() -> dict:
    s = load_settings()
    return s.setdefault("_ttt_stats", {})

def _ttt_stats_save(stats: dict) -> None:
    s = load_settings()
    s["_ttt_stats"] = stats
    save_settings(s)

def _ttt_stats_bump(chat_id: int, user_id: int, name: str, key: str):
    stats = _ttt_stats_load()
    c = stats.setdefault(str(chat_id), {})
    u = c.setdefault(str(user_id), {"name": name or f"ID {user_id}", "wins": 0, "draws": 0, "losses": 0})
    u["name"] = name or u["name"]
    u[key] = int(u.get(key, 0)) + 1
    _ttt_stats_save(stats)

def _ttt_stats_record_winloss(chat_id: int, winner_id: int, winner_name: str, loser_id: int, loser_name: str):
    _ttt_stats_bump(chat_id, winner_id, winner_name, "wins")
    _ttt_stats_bump(chat_id, loser_id, loser_name, "losses")

def _ttt_stats_record_draw(chat_id: int, uid_a: int, name_a: str, uid_b: int, name_b: str):
    _ttt_stats_bump(chat_id, uid_a, name_a, "draws")
    _ttt_stats_bump(chat_id, uid_b, name_b, "draws")

def _ttt_stats_top(chat_id: int, metric: str = "wins", limit: int = 10) -> str:
    metric = metric.lower()
    if metric not in ("wins", "draws", "losses"):
        metric = "wins"
    stats = _ttt_stats_load().get(str(chat_id), {})
    if not stats:
        return "Aún no hay partidas registradas en este chat."
    rows = []
    for uid, rec in stats.items():
        rows.append((int(rec.get(metric, 0)), rec.get("name", f"ID {uid}"), int(uid)))
    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[:limit]
    title = {"wins": "🏆 Top victorias", "draws": "🤝 Top empates", "losses": "💀 Top derrotas"}[metric]
    out = [f"{title} — Tres en raya"]
    for i, (val, name, _uid) in enumerate(rows, start=1):
        out.append(f"{i}. {name} — {val}")
    return "\n".join(out)


# =========================
# TRES EN RAYA (handlers)
def _ttt_get_game(chat_id: int, msg_id: int) -> Dict[str, Any] | None:
    return TTT_GAMES.get(chat_id, {}).get(msg_id)

def _ttt_set_game(chat_id: int, msg_id: int, data: Dict[str, Any]) -> None:
    TTT_GAMES.setdefault(chat_id, {})[msg_id] = data

def _ttt_del_game(chat_id: int, msg_id: int) -> None:
    if chat_id in TTT_GAMES and msg_id in TTT_GAMES[chat_id]:
        del TTT_GAMES[chat_id][msg_id]
        if not TTT_GAMES[chat_id]:
            del TTT_GAMES[chat_id]

def _ttt_new_board() -> list[str]:
    return [TTT_EMPTY] * 9

def _ttt_winner(board: list[str]) -> str | None:
    wins = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6)
    ]
    for a, b, c in wins:
        if board[a] != TTT_EMPTY and board[a] == board[b] == board[c]:
            return board[a]
    return None

def _ttt_full(board: list[str]) -> bool:
    return all(c != TTT_EMPTY for c in board)

def _ttt_board_markup(chat_id: int, msg_id: int, board: list[str], playing: bool) -> InlineKeyboardMarkup:
    rows = []
    for r in range(3):
        btns = []
        for c in range(3):
            idx = r * 3 + c
            label = board[idx]
            if label == TTT_EMPTY and playing:
                cb = f"ttt:play:{chat_id}:{msg_id}:{idx}"
            else:
                cb = f"ttt:nop:{chat_id}:{msg_id}:{idx}"
            btns.append(InlineKeyboardButton(label, callback_data=cb))
        rows.append(btns)
    return InlineKeyboardMarkup(rows)

def _ttt_header_text(state: Dict[str, Any]) -> str:
    pX = state["players"].get("X_name", "X")
    pO = state["players"].get("O_name", "O")
    turn = state.get("turn", "X")
    status = state.get("status")
    if status == "waiting":
        return f"Tres en raya — Esperando oponente…\n{pX} juega con {TTT_X}."
    if status == "playing":
        now = pX if turn == "X" else pO
        return f"Tres en raya — Turno de {now} ➡️"
    if status == "ended":
        result = state.get("result", "fin de partida")
        return f"Tres en raya — {result}"
    return "Tres en raya"

def _ttt_footer_markup(chat_id: int, msg_id: int, state: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    if state["status"] == "waiting":
        buttons.append([InlineKeyboardButton("Unirme", callback_data=f"ttt:join:{chat_id}:{msg_id}")])
        buttons.append([InlineKeyboardButton("Cancelar", callback_data=f"ttt:cancel:{chat_id}:{msg_id}")])
    elif state["status"] == "ended":
        buttons.append([InlineKeyboardButton("Nueva partida", callback_data=f"ttt:rematch:{chat_id}:{msg_id}")])

    board_kb = _ttt_board_markup(chat_id, msg_id, state["board"], state["status"] == "playing")
    all_rows = [list(row) for row in board_kb.inline_keyboard]
    all_rows.extend(buttons)
    return InlineKeyboardMarkup(all_rows)

def _ttt_can_play(state: Dict[str, Any], user_id: int) -> bool:
    if state["status"] != "playing":
        return False
    symbol = state["turn"]
    pid = state["players"].get(f"{symbol}_id")
    return pid == user_id

async def ttt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ttt  o  /tres
    - Si respondes a un mensaje, desafías a ese usuario.
    - Si pasas @usuario en args, reto directo.
    - Si no, partida abierta (botón Unirme).
    """
    msg = update.message
    chat = msg.chat

    # Respect module toggle
    if not is_module_enabled(chat.id, "ttt_enabled"):
        return await msg.reply_text("El módulo TTT está desactivado en este chat.")

    pX = msg.from_user
    opponent_id = None
    opponent_name = None

    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id != pX.id:
        opponent_id = msg.reply_to_message.from_user.id
        opponent_name = msg.reply_to_message.from_user.first_name
    elif context.args and context.args[0].startswith("@"):
        username = context.args[0][1:].lower()
        roster = load_roster().get(str(chat.id), {})
        uid = None
        for uid_str, info in roster.items():
            name = str(info.get("name") or "").strip()
            if name.startswith("@") and name[1:].lower() == username:
                uid = int(uid_str)
                break
        if uid:
            member = await context.bot.get_chat_member(chat.id, uid)
            opponent_id = member.user.id
            opponent_name = member.user.first_name

    state = {
        "board": _ttt_new_board(),
        "status": "waiting",
        "turn": "X",
        "players": {
            "X_id": pX.id,
            "X_name": pX.first_name,
            "O_id": opponent_id,
            "O_name": opponent_name,
        },
        "created_ts": time.time()
    }

    text = _ttt_header_text(state)

    # Envia sin teclado para obtener message_id real (evita msg_id=0)
    sent = await context.bot.send_message(chat_id=chat.id, text=text)

    # Guarda estado con el message_id correcto
    _ttt_set_game(chat.id, sent.message_id, state)

    # Si había oponente, arranca directamente
    if opponent_id and opponent_id != pX.id:
        state["status"] = "playing"
        state["turn"] = random.choice(["X", "O"])
        _ttt_set_game(chat.id, sent.message_id, state)

    # Ahora sí, añade teclado con msg_id real
    kb = _ttt_footer_markup(chat.id, sent.message_id, state)
    await sent.edit_text(_ttt_header_text(state), reply_markup=kb)

async def ttt_join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    q = update.callback_query
    user = q.from_user
    state = _ttt_get_game(chat_id, msg_id)
    if not state:
        return await safe_q_answer(q, "Partida no encontrada.", show_alert=True)
    if state["status"] != "waiting":
        return await safe_q_answer(q, "Esta partida ya comenzó.", show_alert=True)
    if state["players"].get("O_id") and state["players"]["O_id"] != user.id:
        return await safe_q_answer(q, "Esta partida era un reto a otra persona.", show_alert=True)
    if state["players"]["X_id"] == user.id:
        return await safe_q_answer(q, "No puedes ser tu propio oponente 😅", show_alert=True)

    state["players"]["O_id"] = user.id
    state["players"]["O_name"] = user.first_name
    state["status"] = "playing"
    state["turn"] = random.choice(["X", "O"])
    _ttt_set_game(chat_id, msg_id, state)

    await safe_q_answer(q, "¡Partida iniciada!")
    await q.edit_message_text(_ttt_header_text(state), reply_markup=_ttt_footer_markup(chat_id, msg_id, state))

async def ttt_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    q = update.callback_query
    user = q.from_user
    state = _ttt_get_game(chat_id, msg_id)
    if not state:
        return await safe_q_answer(q, "Nada que cancelar.", show_alert=True)
    if state["status"] == "waiting":
        if user.id != state["players"]["X_id"] and not await is_admin(context, chat_id, user.id):
            return await safe_q_answer(q, "No puedes cancelar esta partida.", show_alert=True)
        _ttt_del_game(chat_id, msg_id)
        await safe_q_answer(q)
        return await q.edit_message_text("❌ Partida cancelada.")
    return await safe_q_answer(q, "La partida ya está en curso.", show_alert=True)

async def ttt_rematch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    q = update.callback_query
    old = _ttt_get_game(chat_id, msg_id)
    if not old:
        return await safe_q_answer(q, "No hay partida para reiniciar.", show_alert=True)
    new_state = {
        "board": _ttt_new_board(),
        "status": "playing",
        "turn": random.choice(["X", "O"]),
        "players": {
            "X_id": old["players"]["O_id"],
            "X_name": old["players"]["O_name"],
            "O_id": old["players"]["X_id"],
            "O_name": old["players"]["X_name"],
        },
        "created_ts": time.time()
    }
    _ttt_set_game(chat_id, msg_id, new_state)
    await safe_q_answer(q, "¡Nueva partida!")
    await q.edit_message_text(_ttt_header_text(new_state), reply_markup=_ttt_footer_markup(chat_id, msg_id, new_state))

async def ttt_play_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, idx: int):
    q = update.callback_query
    user = q.from_user
    state = _ttt_get_game(chat_id, msg_id)
    if not state:
        return await safe_q_answer(q, "Partida no encontrada.", show_alert=True)
    if state["status"] != "playing":
        return await safe_q_answer(q, "La partida no está disponible.", show_alert=True)
    if not _ttt_can_play(state, user.id):
        return await safe_q_answer(q, "No es tu turno.", show_alert=True)

    board = state["board"]
    if board[idx] != TTT_EMPTY:
        return await safe_q_answer(q, "Esa casilla ya está ocupada.", show_alert=True)

    symbol = TTT_X if state["turn"] == "X" else TTT_O
    board[idx] = symbol

    winner = _ttt_winner(board)
    if winner:
        px = state["players"]["X_name"]
        po = state["players"]["O_name"]
        x_id = state["players"]["X_id"]
        o_id = state["players"]["O_id"]
        if winner == TTT_X:
            ganador, ganador_id = px, x_id
            perdedor, perdedor_id = po, o_id
        else:
            ganador, ganador_id = po, o_id
            perdedor, perdedor_id = px, x_id
        state["status"] = "ended"
        state["result"] = f"¡{ganador} ha ganado!"
        if ganador_id and perdedor_id:
            _ttt_stats_record_winloss(chat_id, ganador_id, ganador or "Jugador", perdedor_id, perdedor or "Jugador")
    elif _ttt_full(board):
        px = state["players"]["X_name"]
        po = state["players"]["O_name"]
        x_id = state["players"]["X_id"]
        o_id = state["players"]["O_id"]
        state["status"] = "ended"
        state["result"] = "Empate. Buen duelo."
        if x_id and o_id:
            _ttt_stats_record_draw(chat_id, x_id, px or "Jugador X", o_id, po or "Jugador O")
    else:
        state["turn"] = "O" if state["turn"] == "X" else "X"

    _ttt_set_game(chat_id, msg_id, state)
    await safe_q_answer(q)
    await q.edit_message_text(_ttt_header_text(state), reply_markup=_ttt_footer_markup(chat_id, msg_id, state))

# router callbacks del TTT
async def ttt_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        parts = q.data.split(":")
        if parts[0] != "ttt":
            return await safe_q_answer(q)
        action = parts[1]
        chat_id = int(parts[2])
        msg_id = int(parts[3])

        # Denegar callbacks si el módulo está desactivado
        if not is_module_enabled(chat_id, "ttt_enabled"):
            return await safe_q_answer(q, "El módulo TTT está desactivado en este chat.", show_alert=True)

        if action == "play":
            idx = int(parts[4])
            return await ttt_play_cb(update, context, chat_id, msg_id, idx)
        elif action == "join":
            return await ttt_join_cb(update, context, chat_id, msg_id)
        elif action == "cancel":
            return await ttt_cancel_cb(update, context, chat_id, msg_id)
        elif action == "rematch":
            return await ttt_rematch_cb(update, context, chat_id, msg_id)
        else:
            return await safe_q_answer(q)
    except Exception:
        logging.exception("ttt router error")
        try:
            await safe_q_answer(q, "Error en la jugada.", show_alert=True)
        except Exception:
            pass


# =========================
# TOP TTT
async def top_ttt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /top_ttt [wins|draws|losses]
    Muestra el ranking del chat para la métrica indicada (por defecto: wins).
    """
    msg = update.message
    metric = (context.args[0].lower() if context.args else "wins")
    await context.bot.send_message(chat_id=msg.chat.id, text=_ttt_stats_top(msg.chat.id, metric))


# =========================
# TRIVIA MODULE
# =========================

# Trivia file paths
TRIVIA_POOL_FILE = os.path.join(PERSIST_DIR, "pool.json")
TRIVIA_BACKUPS_DIR = os.path.join(PERSIST_DIR, "backups")
TRIVIA_STATE_FILE = os.path.join(PERSIST_DIR, "trivia_state.json")
TRIVIA_STATS_FILE = os.path.join(PERSIST_DIR, "trivia_stats.json")
TRIVIA_ADMIN_LOG_FILE = os.path.join(PERSIST_DIR, "trivia_admin_log.json")

def _ensure_trivia_files() -> None:
    """Ensure all trivia files and directories exist."""
    try:
        # Ensure PERSIST_DIR exists
        os.makedirs(PERSIST_DIR, exist_ok=True)
        
        # Ensure backups directory exists
        os.makedirs(TRIVIA_BACKUPS_DIR, exist_ok=True)
        
        # Create pool.json if it doesn't exist
        if not os.path.exists(TRIVIA_POOL_FILE):
            with open(TRIVIA_POOL_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        
        # Create trivia_state.json if it doesn't exist
        if not os.path.exists(TRIVIA_STATE_FILE):
            with open(TRIVIA_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        
        # Create trivia_stats.json if it doesn't exist
        if not os.path.exists(TRIVIA_STATS_FILE):
            with open(TRIVIA_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        
        # Create trivia_admin_log.json if it doesn't exist
        if not os.path.exists(TRIVIA_ADMIN_LOG_FILE):
            with open(TRIVIA_ADMIN_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception("Error ensuring trivia files", exc_info=e)

def load_pool() -> List[Dict[str, Any]]:
    """Load the trivia question pool from pool.json."""
    _ensure_trivia_files()
    try:
        with open(TRIVIA_POOL_FILE, "r", encoding="utf-8") as f:
            pool = json.load(f)
            if not isinstance(pool, list):
                logging.warning("pool.json is not a list, returning empty pool")
                return []
            return pool
    except Exception as e:
        logging.exception("Error loading pool", exc_info=e)
        return []

def save_pool(pool: List[Dict[str, Any]]) -> None:
    """Save the trivia question pool to pool.json."""
    _ensure_trivia_files()
    try:
        with open(TRIVIA_POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception("Error saving pool", exc_info=e)

def _backup_pool() -> str | None:
    """Create a timestamped backup of pool.json and return the backup path."""
    _ensure_trivia_files()
    try:
        # Read current pool
        pool = load_pool()
        
        # Create backup filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"pool_backup_{timestamp}.json"
        backup_path = os.path.join(TRIVIA_BACKUPS_DIR, backup_filename)
        
        # Save backup
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        
        logging.info(f"Pool backed up to {backup_path}")
        return backup_path
    except Exception as e:
        logging.exception("Error creating pool backup", exc_info=e)
        return None

def _validate_pool_list(pool: List[Dict[str, Any]]) -> tuple[bool, str]:
    """
    Validate that the pool list is correctly formatted.
    Returns (is_valid, error_message).
    """
    if not isinstance(pool, list):
        return False, "Pool must be a list"
    
    for idx, question in enumerate(pool):
        if not isinstance(question, dict):
            return False, f"Question {idx} is not a dictionary"
        
        # Check required fields
        if "id" not in question:
            return False, f"Question {idx} missing 'id' field"
        if "question" not in question:
            return False, f"Question {idx} missing 'question' field"
        if "choices" not in question:
            return False, f"Question {idx} missing 'choices' field"
        if "answer" not in question:
            return False, f"Question {idx} missing 'answer' field"
        
        # Validate types
        if not isinstance(question["id"], int):
            return False, f"Question {idx}: 'id' must be an integer"
        if not isinstance(question["question"], str):
            return False, f"Question {idx}: 'question' must be a string"
        if not isinstance(question["choices"], list):
            return False, f"Question {idx}: 'choices' must be a list"
        if not isinstance(question["answer"], int):
            return False, f"Question {idx}: 'answer' must be an integer"
        
        # Validate choices
        if len(question["choices"]) < 2:
            return False, f"Question {idx}: must have at least 2 choices"
        for choice_idx, choice in enumerate(question["choices"]):
            if not isinstance(choice, str):
                return False, f"Question {idx}, choice {choice_idx}: must be a string"
        
        # Validate answer index
        if question["answer"] < 0 or question["answer"] >= len(question["choices"]):
            return False, f"Question {idx}: answer index {question['answer']} out of range (0-{len(question['choices'])-1})"
    
    return True, ""

def _log_admin_action(chat_id: int, user_id: int, user_name: str, action: str, details: str = "") -> None:
    """Log an admin action to trivia_admin_log.json."""
    _ensure_trivia_files()
    try:
        with open(TRIVIA_ADMIN_LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
            if not isinstance(log, list):
                log = []
    except Exception:
        log = []
    
    entry = {
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
        "details": details
    }
    log.append(entry)
    
    try:
        with open(TRIVIA_ADMIN_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception("Error saving admin log", exc_info=e)

async def trivia_import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /trivia_import - Import trivia questions from JSON (admin only).
    Usage: /trivia_import <JSON data>
    or reply to a message containing JSON with /trivia_import
    
    JSON format:
    [
      {
        "id": 1,
        "question": "What is 2+2?",
        "choices": ["3", "4", "5"],
        "answer": 1
      },
      ...
    ]
    """
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    
    # Check if trivia module is enabled
    if not is_module_enabled(chat.id, "trivia_enabled"):
        return await msg.reply_text("El módulo Trivia está desactivado en este chat.")
    
    # Admin only
    if not await is_admin(context, chat.id, user.id):
        return await msg.reply_text("Este comando es solo para administradores.")
    
    # Get JSON data from command args or replied message
    json_text = None
    if msg.reply_to_message and msg.reply_to_message.text:
        json_text = msg.reply_to_message.text.strip()
    elif context.args:
        json_text = " ".join(context.args).strip()
    
    if not json_text:
        usage = (
            "❌ Uso: /trivia_import <JSON>\n"
            "o responde a un mensaje con JSON usando /trivia_import\n\n"
            "Formato esperado:\n"
            "<code>[\n"
            '  {"id": 1, "question": "¿2+2?", "choices": ["3", "4", "5"], "answer": 1},\n'
            '  {"id": 2, "question": "¿Capital de España?", "choices": ["Madrid", "Barcelona"], "answer": 0}\n'
            "]</code>"
        )
        return await msg.reply_text(usage, parse_mode="HTML")
    
    # Try to parse JSON
    try:
        imported_pool = json.loads(json_text)
    except json.JSONDecodeError as e:
        return await msg.reply_text(f"❌ Error al parsear JSON: {str(e)}")
    
    # Validate the pool structure
    is_valid, error_msg = _validate_pool_list(imported_pool)
    if not is_valid:
        return await msg.reply_text(f"❌ Validación fallida: {error_msg}")
    
    # Backup current pool before importing
    backup_path = _backup_pool()
    backup_info = f"Backup guardado en: {os.path.basename(backup_path)}" if backup_path else "⚠️ No se pudo crear backup"
    
    # Save the new pool
    save_pool(imported_pool)
    
    # Log the action
    _log_admin_action(
        chat.id, 
        user.id, 
        user.first_name or "Admin",
        "trivia_import",
        f"Imported {len(imported_pool)} questions"
    )
    
    success_msg = (
        f"✅ Pool importado exitosamente.\n"
        f"📊 Total de preguntas: {len(imported_pool)}\n"
        f"💾 {backup_info}"
    )
    await msg.reply_text(success_msg)


# =========================
# ON MESSAGE
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or not msg.text:
        return
    chat = msg.chat
    user = msg.from_user

    try:
        cfg = _with_defaults(get_chat_settings(chat.id))
        if cfg.get("notify_name_change", False):
            changes = _detect_name_changes(chat.id, user)
            if changes.get("changed"):
                parts = []
                mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "usuario")}</a>'
                if changes.get("old_first") != changes.get("new_first"):
                    old = html.escape(changes.get("old_first") or "—")
                    new = html.escape(changes.get("new_first") or "—")
                    parts.append(f"🪪 {mention} ha cambiado su nombre: <b>{old}</b> → <b>{new}</b>")
                if changes.get("old_user") != changes.get("new_user"):
                    oldu = ("@" + changes.get("old_user")) if changes.get("old_user") else "—"
                    newu = ("@" + changes.get("new_user")) if changes.get("new_user") else "—"
                    parts.append(f"🔁 Nuevo @usuario: <code>{oldu}</code> → <code>{newu}</code>")
                if parts:
                    try:
                        await context.bot.send_message(
                            chat_id=chat.id,
                            text="\n".join(parts),
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    # roster
    upsert_roster_member(chat.id, user)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        upsert_roster_member(chat.id, msg.reply_to_message.from_user)

    # evitar doble-proceso tras AFK por texto
    if context.user_data.get("afk_skip_message_id") == msg.message_id:
        context.user_data.pop("afk_skip_message_id", None)
        return

    # avisos AFK a terceros (solo si módulo AFK activado)
    await notify_if_mentioning_afk(update, context)

    # si quien habla estaba AFK -> retorno (solo si módulo AFK activado)
    if is_module_enabled(chat.id, "afk_enabled") and user.id in AFK_USERS:
        info = AFK_USERS.pop(user.id)
        since = info.get("since")
        phrase = choose_return_phrase().format(first=user.first_name)
        if since:
            phrase += " (fuera " + format_duration(time.time() - since) + ")"
        await msg.reply_text(phrase)

    # autoresponder
    if chat.id in AUTO_RESPONDERS and user.id in AUTO_RESPONDERS[chat.id]:
        text = AUTO_RESPONDERS[chat.id][user.id]
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            reply_to_message_id=msg.message_id,
            disable_web_page_preview=True
        )


# =========================
# ERROR HANDLER
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled exception", exc_info=context.error)


# =========================
# /CONFIG — Panel compacto con toggles
MODULES: Dict[str, Dict[str, str]] = {
    "afk": {"key": "afk_enabled", "label": "AFK"},
    "all": {"key": "all_enabled", "label": "@all"},
    "admin": {"key": "admin_enabled", "label": "@admin"},
    "autoresp": {"key": "autoresponder_enabled", "label": "Autoresponder"},
    "ttt": {"key": "ttt_enabled", "label": "TTT"},
    "trivia": {"key": "trivia_enabled", "label": "Trivia"},
    "namechg": {"key": "notify_name_change", "label": "SangMata"},
}

DEFAULTS: Dict[str, bool] = {
    "afk_enabled": True,
    "all_enabled": True,
    "admin_enabled": True,
    "autoresponder_enabled": True,
    "ttt_enabled": True,
    "trivia_enabled": False,
    "notify_name_change": False,
}


# =========================
# TIKTOK DOWNLOADER (AUTO) - using requests
import requests

MODULES["tiktok"] = {"key": "tiktok_enabled", "label": "TikTok"}
DEFAULTS["tiktok_enabled"] = True


def tiktok_downloader(url: str) -> bytes | None:
    try:
        # Resolve redirects (vm.tiktok.com -> long URL)
        real_url = requests.get(url, timeout=15, allow_redirects=True).url

        api = f"https://tikwm.com/api/?url={real_url}"
        r = requests.get(api, timeout=20)
        data = r.json()
        video = data.get("data", {}).get("play")
        if not video:
            return None

        vid = requests.get(video, timeout=20)
        return vid.content
    except Exception:
        return None

async def tiktok_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    if not is_module_enabled(msg.chat.id, "tiktok_enabled"):
        return
    m = re.search(r"(https?://[^\s]*tiktok[^\s]*)", msg.text)
    if not m:
        return
    link = m.group(1)
    try:
        await msg.set_reaction("💩")
    except Exception:
        pass
    vid = tiktok_downloader(link)
    if not vid:
        await msg.reply_text("No pude descargar el vídeo de TikTok.")
        return
    await context.bot.send_video(chat_id=msg.chat.id, video=vid)

def _with_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg or {})
    return out

def build_config_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    cfg = _with_defaults(get_chat_settings(chat_id))

    def b(mod_code: str) -> InlineKeyboardButton:
        info = MODULES[mod_code]
        key = info["key"]
        label = info["label"]
        enabled = bool(cfg.get(key, DEFAULTS.get(key, False)))
        state = "✅" if enabled else "❌"
        return InlineKeyboardButton(f"{label} {state}", callback_data=f"cfg:t:{mod_code}")

    codes = list(MODULES.keys())
    rows = []
    for i in range(0, len(codes), 2):
        chunk = codes[i:i + 2]
        rows.append([b(c) for c in chunk])

    rows.append([
        InlineKeyboardButton("🔄 Refrescar", callback_data="cfg:r"),
        InlineKeyboardButton("✖️ Cerrar", callback_data="cfg:x"),
    ])
    return InlineKeyboardMarkup(rows)

async def _assert_admin_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if await is_admin(context, chat_id, user_id):
        return True
    try:
        if update.callback_query:
            await safe_q_answer(update.callback_query, "Solo administradores.", show_alert=True)
        elif update.message:
            await update.message.reply_text("Solo administradores pueden cambiar la configuración.")
    except Exception:
        pass
    return False

async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat

    if chat.type == ChatType.PRIVATE:
        return await msg.reply_text("Esta configuración es por chat. Usa /config en el grupo donde seas administrador.")

    if not await _assert_admin_or_warn(update, context, chat.id, msg.from_user.id):
        return

    kb = build_config_keyboard(chat.id)
    title = "⚙️ Configuración del chat\nToca para activar/desactivar módulos. Solo administradores."
    await msg.reply_text(title, reply_markup=kb)

async def cfg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    chat = q.message.chat
    user_id = q.from_user.id

    if not await _assert_admin_or_warn(update, context, chat.id, user_id):
        return

    try:
        parts = data.split(":", 2)
        if len(parts) < 2 or parts[0] != "cfg":
            return
        action = parts[1]

        if action == "t":
            mod_code = parts[2]
            info = MODULES.get(mod_code)
            if not info:
                return await safe_q_answer(q, "Módulo desconocido.")
            key = info["key"]
            cfg = _with_defaults(get_chat_settings(chat.id))
            cur = bool(cfg.get(key, DEFAULTS.get(key, False)))
            set_chat_setting(chat.id, key, not cur)
            try:
                await q.message.edit_reply_markup(reply_markup=build_config_keyboard(chat.id))
            except BadRequest:
                pass
            return await safe_q_answer(q, "Guardado.")

        if action == "r":
            try:
                await q.message.edit_reply_markup(reply_markup=build_config_keyboard(chat.id))
            except BadRequest:
                pass
            return await safe_q_answer(q, "Actualizado.")

        if action == "x":
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            return await safe_q_answer(q)

    except Exception:
        logging.exception("Error en cfg_callback")
        try:
            await safe_q_answer(q, "No se pudo guardar. Inténtalo de nuevo.", show_alert=True)
        except Exception:
            pass


# =========================
# /START — HUB en privado
HUB_MODULES = {
    "afk": {"title": "AFK", "desc": "Activa el modo ausente con un mensaje automático y aviso al volver.", "cmds": ["afk [motivo]"]},
    "all": {"title": "@all", "desc": "Menciona a todos los miembros del grupo con control anti-spam.", "cmds": ["@all [motivo]"]},
    "admin": {"title": "@admin", "desc": "Avisa solo al equipo de administradores.", "cmds": ["@admin [motivo]"]},
    "autoresp": {"title": "Autoresponder", "desc": "Respuestas automáticas personalizadas por usuario.", "cmds": ["autoresponder", "autoresponder_off"]},
    "ttt": {"title": "Tres en raya", "desc": "Juega partidas de TTT con el grupo y consulta clasificaciones.", "cmds": ["ttt", "top_ttt"]},
    "trivia": {"title": "Trivia", "desc": "Juego de preguntas programado cada hora (desde 00:30).", "cmds": ["trivia_on", "trivia_off", "trivia_stats"]},
    "namechg": {"title": "SangMata", "desc": "Notifica cambios de nombre y @ cuando la persona habla en el grupo.", "cmds": []},
}

def build_hub_keyboard() -> InlineKeyboardMarkup:
    codes = ["afk", "all", "admin", "autoresp", "ttt", "trivia", "namechg"]
    rows = []
    for i in range(0, len(codes), 2):
        chunk = codes[i:i + 2]
        rows.append([InlineKeyboardButton(HUB_MODULES[c]["title"], callback_data=f"hub:m:{c}") for c in chunk])
    rows.append([InlineKeyboardButton("⚙️ Configuración", callback_data="hub:cfg"), InlineKeyboardButton("📜 Ver comandos", callback_data="hub:help")])
    rows.append([InlineKeyboardButton("❌ Cerrar", callback_data="hub:x")])
    return InlineKeyboardMarkup(rows)

def hub_module_text(code: str) -> str:
    m = HUB_MODULES.get(code)
    if not m:
        return "Módulo desconocido."
    title = m["title"]
    desc = m["desc"]
    cmds = m.get("cmds", [])
    lines = [f"🔹 <b>{title}</b>", desc]
    if cmds:
        lines.append("\n<b>Comandos:</b>")
        for c in cmds:
            cfmt = f"/{c}" if not c.startswith('@') and not c.startswith('/') else c
            lines.append(f"• {cfmt}")
    lines.append("\nActívalo o desactívalo desde /config en tu grupo.")
    return "\n".join(lines)

def build_hub_module_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Volver", callback_data="hub:back")],
        [InlineKeyboardButton("⚙️ Configuración", callback_data="hub:cfg"), InlineKeyboardButton("📜 Ver comandos", callback_data="hub:help")],
        [InlineKeyboardButton("❌ Cerrar", callback_data="hub:x")],
    ])

async def _hub_edit_message(q, text: str, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
    try:
        if q.message and getattr(q.message, "photo", None):
            return await q.message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            return await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
    except Exception:
        try:
            return await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
        except Exception:
            pass

async def hub_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if not data.startswith("hub:"):
        return
    action = data.split(":", 1)[1]
    if action == "back":
        try:
            await _hub_edit_message(q, "Elige un módulo para ver su ayuda:", reply_markup=build_hub_keyboard())
        except Exception:
            pass
        return
    if action == "help":
        fake_update = Update(update.update_id, message=q.message)
        try:
            await help_cmd(fake_update, context)
        except Exception:
            pass
        return
    if action == "cfg":
        try:
            await q.message.reply_text("Abre /config en el grupo donde seas administrador para activar/desactivar módulos.")
        except Exception:
            pass
        return
    if action == "x":
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if action.startswith("m:"):
        code = action.split(":", 1)[1]
        txt = hub_module_text(code)
        try:
            await _hub_edit_message(q, txt, parse_mode="HTML", disable_web_page_preview=True, reply_markup=build_hub_module_keyboard(code))
        except Exception:
            pass
        return


# =========================
# MAIN


# Added inside main via dynamic injection


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    ensure_import_once()

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

    # TRIVIA
    app.add_handler(CommandHandler("trivia_import", trivia_import_cmd))

    # TIKTOK DOWNLOADER
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tiktok_detector), group=1)

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
    register_command("trivia_import", "importa preguntas de trivia desde JSON", admin=True)

    print("🐸 RuruBot iniciado.")
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
