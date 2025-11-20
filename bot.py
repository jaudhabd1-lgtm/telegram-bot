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
# =========================
SETTINGS_CACHE: Dict[str, Any] | None = None
ROSTER_CACHE: Dict[str, Any] | None = None

# =========================
# SETTINGS (por chat)
# =========================
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

def is_spooky(cid: int) -> bool:
    return False

# =========================
# /help dinámico (formato BotFather)
# =========================
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
# =========================
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
# =========================
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

def get_roster_entry(key: str) -> Any:
    roster = load_roster()
    return roster.get(key)

def set_roster_entry(key: str, value: Any) -> None:
    roster = load_roster()
    roster[key] = value
    try:
        save_roster(roster)
    except Exception:
        # si hay error al guardar (p.e. permisos), guardamos sólo en memoria
        ROSTER_CACHE = dict(roster)

# =========================
# ADMIN + ROSTER UTILS
# =========================
def _strip_title(username: str) -> str:
    # remove optional [TTT#] prefix from displayed name
    return re.sub(r"^\[TTT\d+\]\s*", "", username)

def _name(user: Any, mention: bool = False) -> str:
    if not user:
        return "(alguien)"
    if getattr(user, "username", None):
        return "@" + user.username
    full_name = (user.full_name or "").strip() or "(alguien)"
    return html.escape(full_name)

def _name_link(user: Any) -> str:
    if not user:
        return "(alguien)"
    name = _name(user)
    if getattr(user, "id", None):
        return f'<a href="tg://user?id={user.id}">{name}</a>'
    return html.escape(name)

def _short_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _mk_roster_entry(user: Any) -> Dict[str, Any]:
    return {
        "name": _name(user),
        "id": getattr(user, "id", None),
        "user": getattr(user, "username", None),
        "ts": _short_ts(datetime.now(ZoneInfo("Europe/Madrid"))),
    }

def ensure_import_once():
    if not LIST_URL:
        return
    ded_chat, parsed = _import_list(LIST_URL)
    if parsed:
        # only import first time the bot runs (to avoid re-adding on each restart)
        if LIST_IMPORT_ONCE:
            settings = load_settings()
            # if we already imported once before, skip
            if settings.get("imported_once"):
                return
            # otherwise mark that we did and continue to import
            set_chat_setting(0, "imported_once", True)
        # Save deduplicated list to roster
        _save_roster_import_list(ded_chat)

# =========================
# COMANDOS: START, HELP, CONFIG
# =========================
HELP_HUB_MODULES: Dict[str, str] = {}

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el mensaje de bienvenida del bot."""
    msg = update.message
    chat = msg.chat
    arg0 = (context.args[0].lower() if context.args else "")
    if arg0 == "help":
        # shortcut: "/start help" para abrir panel de ayuda directamente
        await config_cmd(update, context)
        return
    # Mensaje de bienvenida
    text = (
        "<b>Hola!</b> Soy RuruBot 🐸\n"
        "Aquí tienes la lista de cosas que sé hacer. Usa /help para "
        "ver los comandos disponibles.\n"
    )
    await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista los comandos disponibles (dinámico)."""
    msg = update.message
    # Formateo tipo BotFather
    text = format_commands_list_botfather()
    await msg.reply_text(text)

async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el panel de configuración interactivo."""
    msg = update.message
    chat = msg.chat
    # Teclado inline con secciones (hub)
    keyboard = build_hub_main_keyboard()
    text = "<b>Configuración del bot</b>\n<i>Ajustes disponibles para este chat:</i>"
    await msg.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

async def callback_show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de comandos (callback del botón 'Volver' en el hub)."""
    query = update.callback_query
    text = format_commands_list_botfather()
    await safe_q_answer(query)
    await query.edit_message_text(text)

# =========================
# COMANDOS: AFK
# =========================
AFK_TAG = "💤"  # Tag para nombre
AFK_TIMEOUT = 60 * 60 * 3  # 3 horas
AFK_MENTION_REFRESH = 5 * 60  # 5 min (cada cuánto se "resetea" la mención)

async def afk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa modo AFK (ausente) para el usuario."""
    msg = update.message
    user = msg.from_user
    motivo = " ".join(context.args) if context.args else ""
    if not user or user.id is None:
        return
    # Marcar usuari com AFK amb timestamp
    AFK_USERS[user.id] = {"ts": time.time(), "motivo": motivo}
    name = user.full_name
    try:
        # Afegir tag al nom (només visible per al bot)
        await msg.chat.set_chat_title(f"{name} {AFK_TAG}")
    except Exception:
        # No tots els xats permeten canviar el títol (p. ex. converses privades)
        pass
    # Confirmació al propi usuari
    respuesta = "Estás en modo AFK."
    if motivo:
        respuesta += f" Motivo: {motivo}"
    await msg.reply_text(respuesta)

async def afk_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta missatges de l'usuari per desactivar AFK."""
    msg = update.message
    user = msg.from_user
    if not user or user.id is None:
        return
    # Si l'usuari estava marcat com AFK i envia un missatge, el traiem
    if user.id in AFK_USERS:
        AFK_USERS.pop(user.id, None)
        try:
            # Treure tag del nom
            fullname = user.full_name or ""
            await msg.chat.set_chat_title(fullname)
        except Exception:
            pass
        await msg.reply_text("Has vuelto del modo AFK. ¡Bienvenido de nuevo!")

# =========================
# COMANDOS: HORA
# =========================
# Base de datos de zonas horarias por país
PAISES_TZ = {
    # [Listado de países y sus zonas horarias...]
    "españa": "Europe/Madrid",
    "spain": "Europe/Madrid",
    "cat": "Europe/Madrid",
    "cataluña": "Europe/Madrid",
    "euskal": "Europe/Madrid",
    "gal": "Europe/Madrid",
    "mexico": "America/Mexico_City",
    "méxico": "America/Mexico_City",
    "peru": "America/Lima",
    "usa": "America/New_York",
    "estados unidos": "America/New_York",
    "arg": "America/Argentina/Buenos_Aires",
    "chile": "America/Santiago",
    "japon": "Asia/Tokyo",
    "japón": "Asia/Tokyo",
    # ... (más países)
}

DEFAULT_TZ = "Europe/Madrid"

async def hora_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la hora actual en el país indicado (por defecto España)."""
    msg = update.message
    chat = msg.chat
    # Determinar país (target) del argumento
    if context.args:
        pais = " ".join(context.args).lower()
    else:
        pais = "españa"
    tz = None
    if pais in PAISES_TZ:
        tz = PAISES_TZ[pais]
    else:
        # Intentar deducir a partir del nom del país
        try:
            # Convertir a código de país de dos lletres
            country_code = coco.convert(names=[pais], to='ISO2', not_found=None)
            if country_code:
                tz = pytz.country_timezones(country_code)[0]
        except Exception:
            tz = None
    if not tz:
        await msg.reply_text(f"No reconozco “{pais}”. Prueba con otro país o código.")
        return
    # Obtenir hora actual
    tzinfo = pytz.timezone(tz)
    ahora = datetime.now(tzinfo)
    hora_formateada = ahora.strftime("%H:%M:%S")
    await msg.reply_text(f"Hora en {pais.title()}: {hora_formateada}")

async def hora_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a frases tipo 'hora [país]?'."""
    msg = update.message
    text = msg.text or ""
    # Tractar de trobar una paraula que coincideixi amb algun país conegut
    palabras = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", text.lower())
    pais = None
    for palabra in palabras:
        if palabra in PAISES_TZ:
            pais = palabra
            break
    if not pais:
        return  # no s'ha trobat cap país
    tz = PAISES_TZ[pais]
    tzinfo = pytz.timezone(tz)
    ahora = datetime.now(tzinfo)
    hora_formateada = ahora.strftime("%H:%M:%S")
    await msg.reply_text(f"Hora en {pais.title()}: {hora_formateada}")

# =========================
# COMANDOS: AUTORESPONDER
# =========================
async def autoresponder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa un auto-respuesta para un usuario (admin only)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    if not user or user.id is None:
        return
    if not await is_admin(context, chat.id, user.id):
        await msg.reply_text("Solo un administrador puede usar este comando.")
        return
    if len(context.args) < 2:
        await msg.reply_text("Uso: /autoresponder @usuario texto de respuesta")
        return
    target_username = context.args[0].lstrip("@")
    respuesta = " ".join(context.args[1:])
    if not respuesta:
        await msg.reply_text("Debes especificar el texto de respuesta.")
        return
    # Buscar miembro del chat por username
    try:
        miembro = await chat.get_member(user_id=int(target_username))
    except ValueError:
        miembro = await chat.get_member(user_id=target_username)
    except Exception:
        miembro = None
    if not miembro:
        await msg.reply_text("Usuario no encontrado en el chat.")
        return
    target_id = miembro.user.id
    if chat.id not in AUTO_RESPONDERS:
        AUTO_RESPONDERS[chat.id] = {}
    AUTO_RESPONDERS[chat.id][target_id] = respuesta
    await msg.reply_text(f"Auto-respuesta activada para @{target_username}.")

async def autoresponder_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Desactiva el autoresponder de un usuario (admin only)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    if not user or user.id is None:
        return
    if not await is_admin(context, chat.id, user.id):
        await msg.reply_text("Solo un administrador puede usar este comando.")
        return
    if len(context.args) < 1:
        await msg.reply_text("Uso: /autoresponder_off @usuario")
        return
    target_username = context.args[0].lstrip("@")
    # Buscar miembro del chat per username
    try:
        miembro = await chat.get_member(user_id=int(target_username))
    except ValueError:
        miembro = await chat.get_member(user_id=target_username)
    except Exception:
        miembro = None
    if not miembro:
        await msg.reply_text("Usuario no encontrado en el chat.")
        return
    target_id = miembro.user.id
    if chat.id in AUTO_RESPONDERS and target_id in AUTO_RESPONDERS[chat.id]:
        AUTO_RESPONDERS[chat.id].pop(target_id, None)
        await msg.reply_text(f"Auto-respuesta desactivada para @{target_username}.")
    else:
        await msg.reply_text(f"No hay auto-respuesta activa para @{target_username}.")

# Handler para detectar mensajes y aplicar autoresponder si corresponde
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revisa cada mensaje por si debe responder automáticamente."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    if not user or user.id is None or chat.id is None:
        return
    # Comprobar si hay una auto-respuesta configurada para aquest usuari
    if chat.id in AUTO_RESPONDERS and user.id in AUTO_RESPONDERS[chat.id]:
        respuesta = AUTO_RESPONDERS[chat.id][user.id]
        try:
            # Responder al mensaje original del usuario
            await msg.reply_text(respuesta)
        except Exception:
            pass

# =========================
# COMANDOS: @ALL i @ADMIN
# =========================
ALL_COOLDOWN = 5 * 60  # 5 minutos entre usos de @all
ALL_TIMEOUT = 30  # 30 segundos para confirmar @all
ADMIN_COOLDOWN = 5 * 60  # 5 minutos entre usos de @admin
ADMIN_TIMEOUT = 30  # 30 segundos para confirmar @admin

async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menciona a todos los miembros del grupo (admin only)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    motivo = " ".join(context.args) if context.args else ""
    if not user or user.id is None:
        return
    # Comprobar permisos de admin
    if not await is_admin(context, chat.id, user.id):
        await msg.reply_text("Solo los administradores pueden usar este comando.")
        return
    # Cooldown por chat
    last = _last_all.get(chat.id)
    ahora = time.time()
    if last and (ahora - last) < ALL_COOLDOWN:
        await msg.reply_text("Espera antes de volver a usar /all.")
        return
    _last_all[chat.id] = ahora
    # Construir mensaje mencionando a todos
    miembros = await chat.get_members() if hasattr(chat, "get_members") else []
    menciones = " ".join(_name(m.user) for m in miembros if m.user)
    texto = f"📢 @all {motivo}\n{menciones}"
    # Enviar con confirmación (botón de cancelar)
    teclado = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("Cancelar", callback_data="allconfirm:cancel")
    )
    sent = await msg.reply_text(texto, reply_markup=teclado)
    # Guardar estado de confirmación
    _admin_last[chat.id] = {"tipo": "all", "msg_id": sent.message_id}

async def callback_allconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para confirmar o cancelar la mención @all."""
    query = update.callback_query
    data = query.data.split(":", 1)
    if len(data) != 2:
        return
    action = data[1]
    if action == "cancel":
        # Cancel·lar acció pendent
        await safe_q_answer(query, "Mención @all cancelada.", show_alert=True)
        # Treure teclat de confirmació
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menciona solo a los administradores (admin only)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    motivo = " ".join(context.args) if context.args else ""
    if not user or user.id is None:
        return
    # Comprobar permisos d'admin
    if not await is_admin(context, chat.id, user.id):
        await msg.reply_text("Solo los administradores pueden usar este comando.")
        return
    # Cooldown por chat
    last = _admin_last.get(chat.id)
    ahora = time.time()
    if last and (ahora - last.get("ts", 0)) < ADMIN_COOLDOWN:
        await msg.reply_text("Espera antes de volver a usar /admin.")
        return
    # Mencionar admins
    admins = [member.user for member in (await chat.get_administrators())]
    menciones = " ".join(_name(admin) for admin in admins if admin)
    texto = f"🔔 @admin {motivo}\n{menciones}"
    # Enviar con confirmació (botó de cancelar)
    teclado = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("Cancelar", callback_data="adminconfirm:cancel")
    )
    sent = await msg.reply_text(texto, reply_markup=teclado)
    # Guardar estado de confirmación
    _admin_last[chat.id] = {"tipo": "admin", "ts": time.time(), "msg_id": sent.message_id}

async def callback_adminconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para confirmar o cancelar la mención @admin."""
    query = update.callback_query
    data = query.data.split(":", 1)
    if len(data) != 2:
        return
    action = data[1]
    if action == "cancel":
        # Cancel·lar acció pendent
        await safe_q_answer(query, "Mención @admin cancelada.", show_alert=True)
        # Treure teclat de confirmació
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela una acción pendiente de confirmación (@all o @admin)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    if not user or user.id is None:
        return
    # Només admin
    if not await is_admin(context, chat.id, user.id):
        await msg.reply_text("Solo los administradores pueden usar este comando.")
        return
    # Determinar si hi ha alguna confirmació pendent en aquest xat
    pending = _admin_last.get(chat.id)
    if not pending:
        await msg.reply_text("No hay acciones pendientes para cancelar.")
        return
    tipo = pending.get("tipo")
    msg_id = pending.get("msg_id")
    # Esborrem el missatge original si existeix
    try:
        if msg_id:
            await context.bot.delete_message(chat.id, msg_id)
    except Exception:
        pass
    _admin_last.pop(chat.id, None)
    if tipo == "all":
        await msg.reply_text("Mención @all cancelada.")
    elif tipo == "admin":
        await msg.reply_text("Mención @admin cancelada.")
    else:
        await msg.reply_text("No hay acciones pendientes para cancelar.")

async def mention_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta @all en texto libre para forzar confirmación."""
    msg = update.message
    user = msg.from_user
    chat = msg.chat
    if not user or user.id is None or chat.id is None:
        return
    # Si un usuari envia '@all' sense fer servir el comandament, l'hi avisem
    if "@all" in (msg.text or ""):
        await msg.reply_text("Por favor, usa el comando /all para mencionar a todos (con confirmación).")

async def admin_mention_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta @admin en texto libre para forzar confirmación."""
    msg = update.message
    user = msg.from_user
    chat = msg.chat
    if not user or user.id is None or chat.id is None:
        return
    # Si un usuari envia '@admin' sense fer servir el comandament, l'hi avisem
    if "@admin" in (msg.text or ""):
        await msg.reply_text("Por favor, usa el comando /admin para mencionar admins (con confirmación).")

# =========================
# JUEGO: TRES EN RAYA (Tres en ratlla)
# =========================
TTT_DEFAULT_SIZE = 3  # tamaño del tablero (3x3)
TTT_WIN_LENGTH = 3    # longitud de línea para ganar (3 en raya clásico)

def _ttt_new_game() -> Dict[str, Any]:
    # Crear un nuevo juego de tres en raya vacío
    board = [[TTT_EMPTY for _ in range(TTT_DEFAULT_SIZE)] for _ in range(TTT_DEFAULT_SIZE)]
    return {
        "board": board,
        "turno": TTT_X,
        "jugador_x": None,
        "jugador_o": None,
        "ganador": None,
    }

def _ttt_board_to_str(board: List[List[str]]) -> str:
    # Convertir el tablero a string para mostrar
    lineas = [" ".join(celda for celda in fila) for fila in board]
    return "\n".join(lineas)

def _ttt_check_winner(board: List[List[str]]) -> str | None:
    # Comprobar si hay un ganador en el tablero
    n = TTT_DEFAULT_SIZE
    # Comprobar filas y columnas
    for i in range(n):
        # Fila i
        if board[i][0] != TTT_EMPTY and all(board[i][j] == board[i][0] for j in range(1, n)):
            return board[i][0]
        # Columna i
        if board[0][i] != TTT_EMPTY and all(board[j][i] == board[0][i] for j in range(1, n)):
            return board[0][i]
    # Comprobar diagonales
    if board[0][0] != TTT_EMPTY and all(board[i][i] == board[0][0] for i in range(1, n)):
        return board[0][0]
    if board[0][n-1] != TTT_EMPTY and all(board[i][n-1-i] == board[0][n-1] for i in range(1, n)):
        return board[0][n-1]
    return None

def _ttt_render_game(game: Dict[str, Any]) -> str:
    tablero = _ttt_board_to_str(game["board"])
    turno = game["turno"]
    jugador_x = _name(game["jugador_x"]) if game.get("jugador_x") else "(nadie)"
    jugador_o = _name(game["jugador_o"]) if game.get("jugador_o") else "(nadie)"
    ganador = game.get("ganador")
    texto_ganador = ""
    if ganador:
        texto_ganador = f"\n\n🏆 Ganador: {'❌' if ganador == TTT_X else '⭕'} ¡{_name(game['jugador_x'] if ganador == TTT_X else game['jugador_o'])} gana!"
    return (
        f"<pre>{tablero}</pre>\n"
        f"Turno actual: {'❌' if turno == TTT_X else '⭕'}\n"
        f"Jugador ❌: {jugador_x}\n"
        f"Jugador ⭕: {jugador_o}"
        f"{texto_ganador}"
    )

async def ttt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia una partida de Tres en Raya (alias /tres)."""
    msg = update.message
    chat = msg.chat
    user = msg.from_user
    if not user or user.id is None or chat.id is None:
        return
    # Si ya hay un juego activo en este chat, ignorar
    juegos_chat = TTT_GAMES.get(chat.id, {})
    if any(game for game in juegos_chat.values() if game.get("ganador") is None):
        await msg.reply_text("Ya hay una partida en curso en este chat. Termina la actual antes de iniciar otra.")
        return
    # Crear nuevo juego
    juego = _ttt_new_game()
    juego["jugador_x"] = user
    # Guardar juego
    if chat.id not in TTT_GAMES:
        TTT_GAMES[chat.id] = {}
    sent = await msg.reply_text(_ttt_render_game(juego), parse_mode="HTML", reply_markup=build_ttt_keyboard())
    TTT_GAMES[chat.id][sent.message_id] = juego

async def ttt_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router de callbacks para jugadas de Tres en Raya."""
    query = update.callback_query
    data = query.data.split(":")
    # Formato esperado: "ttt:jugar:x:y"
    if not data or data[0] != "ttt":
        return
    if len(data) < 4:
        return
    accion = data[1]
    if accion != "jugar":
        return
    x_str, y_str = data[2], data[3]
    if not x_str.isdigit() or not y_str.isdigit():
        return
    x, y = int(x_str), int(y_str)
    message = query.message
    chat = message.chat if message else None
    user = update.effective_user
    if not message or not chat or not user:
        return
    juegos_chat = TTT_GAMES.get(chat.id, {})
    juego = juegos_chat.get(message.message_id)
    if not juego:
        # Juego finalizado o no encontrado
        await safe_q_answer(query, "La partida ya no está disponible.", show_alert=True)
        return
    ganador = juego.get("ganador")
    if ganador:
        # Si la partida ja té guanyador, ignorar jugades addicionals
        await safe_q_answer(query)
        return
    turno_actual = juego["turno"]
    # Determinar símbolo del usuario (si participa)
    simbolo_usuario = None
    if juego.get("jugador_x") and juego["jugador_x"].id == user.id:
        simbolo_usuario = TTT_X
    elif juego.get("jugador_o") and juego["jugador_o"].id == user.id:
        simbolo_usuario = TTT_O
    # Si el usuario no está asignado, asígnale el símbolo que toca
    if not simbolo_usuario:
        if turno_actual == TTT_X:
            juego["jugador_x"] = user
            simbolo_usuario = TTT_X
        else:
            juego["jugador_o"] = user
            simbolo_usuario = TTT_O
    # Comprobar si és el seu torn
    if simbolo_usuario != turno_actual:
        await safe_q_answer(query, "¡Espera tu turno!", show_alert=True)
        return
    # Realizar la jugada
    tablero = juego["board"]
    if tablero[x][y] != TTT_EMPTY:
        await safe_q_answer(query, "Esa posición ya está ocupada.", show_alert=True)
        return
    tablero[x][y] = simbolo_usuario
    # Comprobar si guanya
    ganador = _ttt_check_winner(tablero)
    if ganador:
        juego["ganador"] = ganador
    else:
        # Cambiar turno
        juego["turno"] = TTT_O if turno_actual == TTT_X else TTT_X
    # Actualizar mensaje del juego
    try:
        await query.edit_message_text(_ttt_render_game(juego), parse_mode="HTML", reply_markup=build_ttt_keyboard())
    except Exception:
        pass

# =========================
# JUEGO: TECLADOS DE TRES EN RAYA
# =========================
def build_ttt_keyboard() -> InlineKeyboardMarkup:
    # Construir un teclado inline 3x3 para jugar Tres en Raya
    keyboard: List[List[InlineKeyboardButton]] = []
    for i in range(TTT_DEFAULT_SIZE):
        fila: List[InlineKeyboardButton] = []
        for j in range(TTT_DEFAULT_SIZE):
            boton = InlineKeyboardButton(" ", callback_data=f"ttt:jugar:{i}:{j}")
            fila.append(boton)
        keyboard.append(fila)
    return InlineKeyboardMarkup(keyboard)

# =========================
# MÓDULO: HUB DE AJUSTES (CONFIG)
# =========================
HUB_MODULES: Dict[str, str] = {}  # Clau: codi, Valor: titol

def register_hub_module(code: str, title: str) -> None:
    HUB_MODULES[code] = title

def build_hub_main_keyboard() -> InlineKeyboardMarkup:
    # Teclat principal del hub de configuració: un botó per cada mòdul disponible
    botones = []
    for codigo, titulo in HUB_MODULES.items():
        botones.append(InlineKeyboardButton(titulo, callback_data=f"hub:{codigo}"))
    # Afegir botó per tancar/ocultar
    botones.append(InlineKeyboardButton("❌ Tancar", callback_data="hub:close"))
    keyboard = InlineKeyboardMarkup.from_column(botones)
    return keyboard

def build_hub_module_keyboard(selected: str) -> InlineKeyboardMarkup:
    # Teclat per als sub-mòduls (es mostra en prémer un botó del hub principal)
    botones = []
    for codigo, titulo in HUB_MODULES.items():
        # Botó de la secció seleccionada actualment
        if codigo == selected:
            botones.append(InlineKeyboardButton(f"· {titulo} ·", callback_data=f"hub:{codigo}"))
        else:
            botones.append(InlineKeyboardButton(titulo, callback_data=f"hub:{codigo}"))
    # Afegir botó per tornar enrere
    botones.append(InlineKeyboardButton("« Volver", callback_data="show_help"))
    keyboard = InlineKeyboardMarkup.from_column(botones)
    return keyboard

# =========================
# COMANDOS: MÓDULOS DE AJUSTES (dinámicos)
# =========================
# (Aquí podríamos registrar dinámicamente subcomandos / módulos de configuración adicionales)

# Ejemplo de registro de módulos de configuración:
register_hub_module("mod1", "Módulo 1")
register_hub_module("mod2", "Módulo 2")
# ...

async def hub_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router de callbacks para el hub de configuración."""
    query = update.callback_query
    data = query.data.split(":", 1)
    if len(data) != 2:
        return
    action = data[1]
    # Tancar el hub
    if action == "close":
        await safe_q_answer(query)
        try:
            # Esborrar teclat (ocultar panel de config)
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    # Canviar de mòdul
    if action in HUB_MODULES:
        # Carregar text del mòdul (aquí podria anar la lògica específica de cada mòdul)
        txt = hub_module_text(action)
        try:
            # Editar el missatge per mostrar el contingut del mòdul i el teclat actualitzat
            await query.edit_message_text(txt, parse_mode="HTML", disable_web_page_preview=True, reply_markup=build_hub_module_keyboard(action))
        except Exception:
            pass

def hub_module_text(code: str) -> str:
    # Retorna el text que s'ha de mostrar per a un mòdul de config donat.
    # (En un cas real, possiblement es carregaria des d'una base de dades o es generaria dinàmicament)
    return f"<b>{HUB_MODULES.get(code, 'Módulo')}</b>\nContenido de la configuración del módulo."

# Handler global per interceptar navegació del hub
async def cfg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercepta todos los callbacks del hub de configuración."""
    query = update.callback_query
    if not query or not query.data:
        return
    if query.data.startswith("hub:"):
        # Deixar que hub_router manegi aquests callbacks
        return
    # Si no és un callback del hub, ignorar
    return

async def _hub_edit_message(query, text: str, **kwargs):
    # Wrapper per a edit_message_text que absorbeix excepcions (p.e. si l'usuari es mou de chat)
    try:
        return await query.edit_message_text(text, **kwargs)
    except Exception as e:
        # Si es produeix un error editant (p.e. missatge esborrat o usuari fora de chat), l'ignorem
        logging.warning(f"Hub edit failed: {e}")
        return None

# =========================
# DESCARGA DE VÍDEOS (TikTok)
# =========================
def _download_video(video_id: str, output_path: str) -> bool:
    # Simulación de descarga de video (TikTok) – Aquí iría la lógica real con API o scraping.
    try:
        # Suposadament descarregar el vídeo i guardar-lo a output_path
        with open(output_path, "wb") as f:
            f.write(b"SIMULATED VIDEO CONTENT")
        return True
    except Exception as e:
        logging.error(f"Error descargando video {video_id}: {e}")
        return False

async def tiktok_detector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta enlaces de TikTok en mensajes de texto y descarga el video."""
    msg = update.message
    text = msg.text or ""
    # Comprobar si el mensaje contiene un enlace de TikTok
    tiktok_urls = re.findall(r"(https?://(?:www\.)?tiktok\.com/\S+)", text)
    if not tiktok_urls:
        return  # No hay enlaces de TikTok
    await msg.reply_text("Descargando video de TikTok...")
    for url in tiktok_urls:
        # Extraer identificador (simulado)
        video_id = url.split("/")[-1]
        output_file = os.path.join(PERSIST_DIR, f"{video_id}.mp4")
        if _download_video(video_id, output_file):
            try:
                await msg.reply_video(video=open(output_file, "rb"))
            except Exception as e:
                logging.error(f"Error enviando video {video_id}: {e}")
                await msg.reply_text(f"Error al enviar el video {video_id}.")
        else:
            await msg.reply_text(f"Error al descargar el video {video_id}.")

# =========================
# HANDLER DE ERRORES
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        await context.bot.send_message(
            chat_id=os.getenv("LOG_CHAT_ID", ""),
            text=f"💥 Se ha producido un error: <code>{html.escape(str(context.error))}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Failed to send error log: {e}")

# =========================
# MAIN
# =========================
# Added inside main via dynamic injection

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    ensure_import_once()

    # START / HELP / HALLOWEEN
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

    print("🐸 RuruBot iniciado.")
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
