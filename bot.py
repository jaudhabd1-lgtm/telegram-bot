# -*- coding: utf-8 -*-
"""
Bot de Telegram (python-telegram-bot v21) con:
- Roster persistente por chat en disco (roster.json), con guardado diferido.
- Importación inicial opcional desde un fichero de lista con formato:
    [NUM_MENSAJES] @usuario_o_nombre [ID]
  Ejemplo de línea: "12 @pepito 123456789"  o  "4 Juan 987654321"
- Actualización automática del roster:
    * Al escribir cualquier usuario (incrementa contador de mensajes).
    * Al unirse nuevos miembros (se añaden al roster).
    * Al abandonar alguien el grupo (se elimina del roster).
- Comando/trigger @all que usa el roster actual para mencionar a todos (humanos) en lotes.
- Soporte para Render: se puede configurar un directorio persistente con la var de entorno PERSIST_DIR.

Variables de entorno relevantes:
- TOKEN (obligatoria)
- LIST_URL (opcional) – URL raw al fichero list_XXXXXXXXXXXX.txt del grupo.
  Por defecto: "https://raw.githubusercontent.com/jaudhabd1-lgtm/telegram-bot/refs/heads/main/list_1002996169471.txt"
- LIST_CHAT_ID (opcional) – ID numérico del grupo para el que corresponde la lista
  (si no se deduce del nombre del fichero de la URL)
- ALL_COOLDOWN (opcional) – segundos de enfriamiento para @all (por defecto 300).

Nota Render / persistencia: Si tu instancia se redeploya, el sistema de archivos se
reinicia. En Render, monta un volumen y exporta PERSIST_DIR (por ejemplo "/data").
Así roster.json sobrevivirá entre reinicios y despliegues.
"""

from __future__ import annotations
import os, json, time, logging, re, html, asyncio
from typing import Dict, Any, List, Tuple
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Falta la variable de entorno TOKEN")

PERSIST_DIR = os.environ.get("PERSIST_DIR", "")
BASE_DIR = PERSIST_DIR if PERSIST_DIR else os.getcwd()
ROSTER_FILE = os.path.join(BASE_DIR, "roster.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
LIST_URL = os.environ.get(
    "LIST_URL",
    "https://raw.githubusercontent.com/jaudhabd1-lgtm/telegram-bot/refs/heads/main/list_1002996169471.txt",
)
# Modo de importación al arrancar: "merge" (fusionar) o "seed" (solo si vacío)
LIST_IMPORT_MODE = os.environ.get("LIST_IMPORT_MODE", "merge").lower()
# Importar solo una vez por chat y marcarlo en settings (por defecto True)
LIST_IMPORT_ONCE = os.environ.get("LIST_IMPORT_ONCE", "true").lower() in {"1","true","yes","y"}
ALL_COOLDOWN = int(os.environ.get("ALL_COOLDOWN", "300"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# =========================
# JSON helpers con guardado diferido
# =========================
_SETTINGS_CACHE: Dict[str, Any] | None = None
_ROSTER_CACHE: Dict[str, Dict[str, Dict[str, Any]]] | None = None
_SETTINGS_DIRTY = False
_ROSTER_DIRTY = False

async def _autosave_job(_: ContextTypes.DEFAULT_TYPE):
    # Guarda (si hay cambios) cada X segundos via JobQueue
    global _SETTINGS_DIRTY, _ROSTER_DIRTY
    if _SETTINGS_DIRTY and _SETTINGS_CACHE is not None:
        _safe_write_json(SETTINGS_FILE, _SETTINGS_CACHE)
        _SETTINGS_DIRTY = False
        log.debug("Settings guardados")
    if _ROSTER_DIRTY and _ROSTER_CACHE is not None:
        _safe_write_json(ROSTER_FILE, _ROSTER_CACHE)
        _ROSTER_DIRTY = False
        log.debug("Roster guardado")

def _safe_read_json(path: str, default: Any):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("No se pudo leer %s: %s", path, e)
        return default

def _safe_write_json(path: str, payload: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# =========================
# SETTINGS por chat
# =========================

def load_settings() -> Dict[str, Any]:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = _safe_read_json(SETTINGS_FILE, {})
    return _SETTINGS_CACHE

def save_settings(s: Dict[str, Any]) -> None:
    global _SETTINGS_CACHE, _SETTINGS_DIRTY
    _SETTINGS_CACHE = s
    _SETTINGS_DIRTY = True

def get_chat_settings(cid: int) -> Dict[str, Any]:
    return load_settings().get(str(cid), {})

def set_chat_setting(cid: int, key: str, value: Any) -> None:
    s = load_settings()
    ckey = str(cid)
    if ckey not in s:
        s[ckey] = {}
    s[ckey][key] = value
    save_settings(s)

# =========================
# ROSTER helpers
# =========================

def load_roster() -> Dict[str, Dict[str, Dict[str, Any]]]:
    global _ROSTER_CACHE
    if _ROSTER_CACHE is None:
        _ROSTER_CACHE = _safe_read_json(ROSTER_FILE, {})
    return _ROSTER_CACHE

def save_roster(r: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    global _ROSTER_CACHE, _ROSTER_DIRTY
    _ROSTER_CACHE = r
    _ROSTER_DIRTY = True

ROSTER_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+(\d+)\s*$")


def _deduce_chat_id_from_url(url: str) -> int | None:
    # Busca list_123456.txt → 123456
    m = re.search(r"list_(\-?\d+)\.txt", url)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    # Alternativa: env LIST_CHAT_ID
    v = os.environ.get("LIST_CHAT_ID")
    if v:
        try:
            return int(v)
        except ValueError:
            return None
    return None


def import_roster_from_list(url: str) -> Tuple[int | None, Dict[str, Dict[str, Any]]]:
    """Devuelve (chat_id_deducido, mapa uid→datos) a partir de la lista remota.
    El formato esperado por línea es: "N @usuario 123" o "N Nombre 123".
    """
    try:
        with urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as e:
        log.warning("No se pudo descargar la lista: %s", e)
        return (None, {})

    entries: Dict[str, Dict[str, Any]] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ROSTER_LINE_RE.match(line)
        if not m:
            continue
        msgs = int(m.group(1))
        mid = m.group(3)
        try:
            uid = str(int(mid))
        except ValueError:
            continue
        middle = m.group(2).strip()
        username = None
        name = middle
        if middle.startswith("@"):
            username = middle[1:]
            name = middle  # guardamos con @ visible en "name" para texto
        entries[uid] = {
            "name": name,
            "username": username,
            "is_bot": False,
            "last_ts": time.time(),
            "messages": msgs,
        }
    return (_deduce_chat_id_from_url(url), entries)


def _merge_roster(existing: Dict[str, Dict[str, Any]], parsed: Dict[str, Dict[str, Any]], strategy: str = "merge") -> Dict[str, Dict[str, Any]]:
    """Fusiona el roster existente con el de la lista.
    - strategy="merge": añade los que faltan; para existentes, conserva datos locales y
      actualiza username/name si están vacíos; "messages" toma el máximo.
    - strategy="overwrite": reemplaza campos con lo de la lista, pero conserva is_bot y last_ts más recientes.
    """
    out = dict(existing)
    for uid, pdata in parsed.items():
        if uid not in out:
            out[uid] = pdata
            continue
        # Ya existe → fusionar
        cur = dict(out[uid])
        if strategy == "overwrite":
            cur.update({
                "name": pdata.get("name", cur.get("name")),
                "username": pdata.get("username", cur.get("username")),
                "messages": pdata.get("messages", cur.get("messages", 0)),
            })
        else:  # merge
            if not cur.get("username") and pdata.get("username"):
                cur["username"] = pdata["username"]
            if not cur.get("name") and pdata.get("name"):
                cur["name"] = pdata["name"]
            cur["messages"] = max(int(cur.get("messages", 0)), int(pdata.get("messages", 0)))
        # timestamps
        cur["last_ts"] = max(float(cur.get("last_ts", 0.0)), float(pdata.get("last_ts", 0.0)))
        # is_bot solo si existe en cur
        cur["is_bot"] = bool(cur.get("is_bot", False))
        out[uid] = cur
    return out

    entries: Dict[str, Dict[str, Any]] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ROSTER_LINE_RE.match(line)
        if not m:
            continue
        msgs = int(m.group(1))
        mid = m.group(3)
        try:
            uid = str(int(mid))
        except ValueError:
            continue
        middle = m.group(2).strip()
        username = None
        name = middle
        if middle.startswith("@"):
            username = middle[1:]
            name = middle  # guardamos con @ visible en "name" para texto
        entries[uid] = {
            "name": name,
            "username": username,
            "is_bot": False,
            "last_ts": time.time(),
            "messages": msgs,
        }
    return (_deduce_chat_id_from_url(url), entries)


def ensure_roster_seeded_from_list():
    """Importa o fusiona desde LIST_URL al arrancar según LIST_IMPORT_MODE.
    Si LIST_IMPORT_ONCE está activo, solo realiza la operación una vez por chat
    y lo marca en settings para evitar futuras fusiones automáticas.
    """
    roster = load_roster()
    if not LIST_URL:
        return
    ded_chat, parsed = import_roster_from_list(LIST_URL)
    if not parsed:
        return
    chat_id = ded_chat
    env_chat = os.environ.get("LIST_CHAT_ID")
    if env_chat:
        try:
            chat_id = int(env_chat)
        except ValueError:
            pass
    if chat_id is None:
        log.warning("No se pudo deducir LIST_CHAT_ID; omito importación inicial")
        return

    # Comprobación de importación única
    if LIST_IMPORT_ONCE:
        cs = get_chat_settings(chat_id)
        if cs.get("list_import_done"):
            log.info("Importación inicial ya marcada como realizada para chat %s; omitida.", chat_id)
            return

    key = str(chat_id)
    existing = roster.get(key, {})
    if LIST_IMPORT_MODE == "seed":
        if not existing:
            roster[key] = parsed
            save_roster(roster)
            set_chat_setting(chat_id, "list_import_done", True)
            log.info("Roster SEED para chat %s desde LIST_URL (%d usuarios)", key, len(parsed))
        else:
            log.info("Roster existente para chat %s – omitida importación inicial (seed)", key)
        return

    # merge por defecto
    merged = _merge_roster(existing, parsed, strategy="merge")
    if merged != existing:
        roster[key] = merged
        save_roster(roster)
        log.info("Roster MERGE para chat %s: +%d usuarios (total %d)", key, max(0, len(merged) - len(existing)), len(merged))
    else:
        log.info("Roster sin cambios tras MERGE para chat %s", key)

    if LIST_IMPORT_ONCE:
        set_chat_setting(chat_id, "list_import_done", True)

# =========================
# Actualizaciones de roster
# =========================

def upsert_roster_member(chat_id: int, user) -> None:
    if not user:
        return
    roster = load_roster()
    key = str(chat_id)
    chat_data = roster.get(key, {})
    uid = str(user.id)
    current = chat_data.get(uid, {})
    chat_data[uid] = {
        "name": user.first_name or (f"@{user.username}" if user.username else "Usuario"),
        "username": user.username,
        "is_bot": bool(getattr(user, "is_bot", False)),
        "last_ts": time.time(),
        "messages": int(current.get("messages", 0)) + 1,
    }
    roster[key] = chat_data
    save_roster(roster)


def remove_roster_member(chat_id: int, user_id: int) -> None:
    roster = load_roster()
    key = str(chat_id)
    chat_data = roster.get(key, {})
    uid = str(user_id)
    if uid in chat_data:
        del chat_data[uid]
        roster[key] = chat_data
        save_roster(roster)
        log.info("Usuario %s eliminado del roster de %s", uid, key)


def get_chat_roster(chat_id: int) -> List[dict]:
    data = load_roster().get(str(chat_id), {})
    out: List[dict] = []
    for uid_str, info in data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        out.append({
            "id": uid,
            "first_name": (info.get("name") or "usuario").strip(),
            "username": info.get("username") or "",
            "is_bot": bool(info.get("is_bot", False)),
        })
    return out


def build_mentions_html(members: List[dict]) -> List[str]:
    """Construye menciones en HTML (20 por bloque)."""
    people = [m for m in members if m.get("id") and not m.get("is_bot")]
    seen = set()
    clean = []
    for u in people:
        uid = u["id"]
        if uid in seen:
            continue
        seen.add(uid)
        name = (u.get("first_name") or u.get("username") or "usuario").strip()
        mention = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
        clean.append(mention)
    chunks, batch = [], []
    for i, m in enumerate(clean, 1):
        batch.append(m)
        if i % 20 == 0:
            chunks.append(", ".join(batch)); batch = []
    if batch:
        chunks.append(", ".join(batch))
    return chunks

# =========================
# Comandos y triggers
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "¡Hola! Mantengo un roster persistente, @all y más. Añádeme a tu grupo y dame permisos para ver miembros.")
    else:
        await update.message.reply_text("Bot listo. Usa /help para ver comandos.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Comandos disponibles:\n"
        "/all [motivo] — Menciona a todos (admins recomendado).\n"
        "/roster_count — Muestra cuántos usuarios hay en roster.\n"
        "Triggers: escribe @all [texto] para lanzar el diálogo de confirmación."
    )
    await update.message.reply_text(txt)


_last_all: Dict[int, float] = {}

async def _can_use_all(chat_id: int) -> bool:
    now = time.time()
    last = _last_all.get(chat_id, 0)
    if now - last < ALL_COOLDOWN:
        return False
    _last_all[chat_id] = now
    return True


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await _can_use_all(chat.id):
        await update.message.reply_text("Debes esperar antes de volver a usar @all.")
        return

    motivo = " ".join(context.args) if context.args else ""
    header = (f"@all por {html.escape(user.first_name)}" + (f": {html.escape(motivo)}" if motivo else ""))

    roster = get_chat_roster(chat.id)
    if not roster:
        await update.message.reply_text("No tengo lista de usuarios para mencionar aquí.")
        return
    chunks = build_mentions_html(roster)
    await update.message.reply_html(header)
    for part in chunks:
        await update.message.reply_html(part, disable_web_page_preview=True)


async def trigger_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    if not text:
        return
    if not text.lower().startswith("@all"):
        return
    # Reutilizamos el comando
    args = text.split()[1:]
    context.args = args
    await cmd_all(update, context)


async def roster_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    data = load_roster().get(str(chat.id), {})
    await update.message.reply_text(f"Usuarios en roster: {len(data)}")


async def import_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Funde manualmente la LIST_URL en el roster del chat actual."""
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Este comando solo funciona en grupos.")
        return
    _, parsed = import_roster_from_list(LIST_URL)
    if not parsed:
        await update.message.reply_text("No pude descargar/parsear la LIST_URL.")
        return
    roster = load_roster()
    key = str(chat.id)
    existing = roster.get(key, {})
    merged = _merge_roster(existing, parsed, strategy="merge")
    roster[key] = merged
    save_roster(roster)
    added = max(0, len(merged) - len(existing))
    await update.message.reply_text(f"Importación completada: añadidos {added} usuarios. Total ahora: {len(merged)}")


async def export_roster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exporta el roster del chat como JSON adjunto."""
    chat = update.effective_chat
    data = load_roster().get(str(chat.id), {})
    path = os.path.join(BASE_DIR, f"roster_{chat.id}.json")
    _safe_write_json(path, data)
    try:
        await update.message.reply_document(document=open(path, "rb"), filename=os.path.basename(path), caption=f"Export del roster ({len(data)} usuarios)")
    except Exception:
        await update.message.reply_text("No he podido adjuntar el archivo. Revisa permisos.")


async def list_import_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    cs = get_chat_settings(chat.id)
    status = "realizada" if cs.get("list_import_done") else "pendiente"
    await update.message.reply_text(f"Estado de importación inicial: {status} (LIST_IMPORT_ONCE={LIST_IMPORT_ONCE})")


async def list_import_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    set_chat_setting(chat.id, "list_import_done", False)
    await update.message.reply_text("Marcador de importación inicial reiniciado. Podrás fusionar de nuevo al arrancar o con /import_list.")

# =========================
# Actualización de roster por actividad
# =========================

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat and user and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        upsert_roster_member(chat.id, user)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    for u in (update.message.new_chat_members or []):
        upsert_roster_member(chat.id, u)

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.message.left_chat_member
    if chat and u:
        remove_roster_member(chat.id, u.id)

# =========================
# main
# =========================

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()
    # Job de guardado diferido
    app.job_queue.run_repeating(_autosave_job, interval=15, first=15)

    # Sembrar roster desde la lista remota (si procede)
    ensure_roster_seeded_from_list()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["all", "todos"], cmd_all))
    app.add_handler(CommandHandler("roster_count", roster_count))
    app.add_handler(CommandHandler("import_list", import_list_cmd))
    app.add_handler(CommandHandler("export_roster", export_roster_cmd))
    app.add_handler(CommandHandler("list_import_status", list_import_status_cmd))
    app.add_handler(CommandHandler("list_import_reset", list_import_reset_cmd))

    # Trigger textual @all
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), trigger_all))

    # Actualización de roster por actividad
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), on_any_message))
    # Altas/bajas
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    return app


def main():
    app = build_app()
    log.info("Bot iniciado. Esperando actualizaciones…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
