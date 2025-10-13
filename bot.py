
from __future__ import annotations
import os, re, json, time, html, logging
from typing import Dict, Any, List, Tuple
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder, Application,
    CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot-parxe-minim")

# =========================
# CONFIG BÀSICA
# =========================
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Falta la variable d'entorn TOKEN")

PERSIST_DIR = os.environ.get("PERSIST_DIR", "/data").strip() or "."
BASE_DIR = PERSIST_DIR if os.path.isdir(PERSIST_DIR) else os.getcwd()
ROSTER_FILE = os.path.join(BASE_DIR, "roster.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

LIST_URL = os.environ.get("LIST_URL", "")
LIST_IMPORT_ONCE = os.environ.get("LIST_IMPORT_ONCE", "true").lower() in {"1","true","yes","y"}
LIST_IMPORT_MODE = os.environ.get("LIST_IMPORT_MODE", "merge").lower()  # merge|seed
ALL_COOLDOWN = int(os.environ.get("ALL_COOLDOWN", "300"))

# =========================
# UTILITATS JSON
# =========================

def _safe_read_json(path: str, default: Any):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("No s'ha pogut llegir %s: %s", path, e)
        return default

def _safe_write_json(path: str, payload: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# =========================
# STATE EN MEMÒRIA + GUARDAT DIFERIT
# =========================
_SETTINGS: Dict[str, Any] = _safe_read_json(SETTINGS_FILE, {})
_ROSTER: Dict[str, Dict[str, Dict[str, Any]]] = _safe_read_json(ROSTER_FILE, {})
_DIRTY_SETTINGS = False
_DIRTY_ROSTER = False

async def _autosave(_: ContextTypes.DEFAULT_TYPE):
    global _DIRTY_SETTINGS, _DIRTY_ROSTER
    if _DIRTY_SETTINGS:
        _safe_write_json(SETTINGS_FILE, _SETTINGS)
        _DIRTY_SETTINGS = False
    if _DIRTY_ROSTER:
        _safe_write_json(ROSTER_FILE, _ROSTER)
        _DIRTY_ROSTER = False

# =========================
# ROSTER HELPERS
# =========================

def _get_chat_settings(chat_id: int) -> Dict[str, Any]:
    return _SETTINGS.get(str(chat_id), {})

def _set_chat_setting(chat_id: int, key: str, value: Any):
    global _DIRTY_SETTINGS
    k = str(chat_id)
    if k not in _SETTINGS:
        _SETTINGS[k] = {}
    _SETTINGS[k][key] = value
    _DIRTY_SETTINGS = True

ROSTER_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+(\-?\d+)\s*$")

def _import_list(url: str) -> Tuple[int|None, Dict[str, Dict[str, Any]]]:
    if not url:
        return (None, {})
    try:
        with urlopen(url, timeout=15) as r:
            content = r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as e:
        log.warning("No s'ha pogut descarregar la llista: %s", e)
        return (None, {})
    parsed: Dict[str, Dict[str, Any]] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
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
        if middle.startswith('@'):
            username = middle[1:]
            name = middle
        parsed[uid] = {
            "name": name,
            "username": username,
            "is_bot": False,
            "last_ts": time.time(),
            "messages": msgs,
        }
    # Intent de deduir chat_id de list_XXXX.txt
    m = re.search(r"list_(\-?\d+)\.txt", url)
    chat_id = int(m.group(1)) if m else None
    return (chat_id, parsed)


def _merge(existing: Dict[str, Dict[str, Any]], incoming: Dict[str, Dict[str, Any]], mode: str = "merge") -> Dict[str, Dict[str, Any]]:
    out = dict(existing)
    for uid, pdata in incoming.items():
        if uid not in out:
            out[uid] = pdata
            continue
        cur = dict(out[uid])
        if mode == "seed":
            # No tocar existents en mode seed
            pass
        else:  # merge
            if not cur.get("username") and pdata.get("username"):
                cur["username"] = pdata["username"]
            if not cur.get("name") and pdata.get("name"):
                cur["name"] = pdata["name"]
            cur["messages"] = max(int(cur.get("messages", 0)), int(pdata.get("messages", 0)))
            cur["last_ts"] = max(float(cur.get("last_ts", 0.0)), float(pdata.get("last_ts", 0.0)))
        out[uid] = cur
    return out


def ensure_import_once():
    """Fa la importació/fusió inicial només una vegada per xat i ho marca a settings."""
    global _ROSTER, _DIRTY_ROSTER
    if not LIST_URL:
        return
    ded_chat, parsed = _import_list(LIST_URL)
    if not parsed:
        return
    if ded_chat is None:
        log.info("No s'ha pogut deduir el chat_id de la LIST_URL; ometent import inicial")
        return
    cs = _get_chat_settings(ded_chat)
    if LIST_IMPORT_ONCE and cs.get("list_import_done"):
        log.info("Import inicial ja feta per al xat %s; ometent.", ded_chat)
        return
    key = str(ded_chat)
    existing = _ROSTER.get(key, {})
    if LIST_IMPORT_MODE == "seed" and existing:
        log.info("Mode seed i ja hi ha roster; no s'importa")
    else:
        merged = _merge(existing, parsed, mode=LIST_IMPORT_MODE)
        _ROSTER[key] = merged
        _DIRTY_ROSTER = True
        log.info("Importació inicial (%s) per al xat %s: total %d usuaris", LIST_IMPORT_MODE, key, len(merged))
    if LIST_IMPORT_ONCE:
        _set_chat_setting(ded_chat, "list_import_done", True)

# =========================
# API ROSTER
# =========================

def _upsert_member(chat_id: int, user) -> None:
    global _DIRTY_ROSTER
    key = str(chat_id)
    chat_data = _ROSTER.get(key, {})
    uid = str(user.id)
    cur = chat_data.get(uid, {})
    chat_data[uid] = {
        "name": user.first_name or (f"@{user.username}" if user.username else "Usuari"),
        "username": user.username,
        "is_bot": bool(getattr(user, "is_bot", False)),
        "last_ts": time.time(),
        "messages": int(cur.get("messages", 0)) + 1,
    }
    _ROSTER[key] = chat_data
    _DIRTY_ROSTER = True

def _remove_member(chat_id: int, user_id: int) -> None:
    global _DIRTY_ROSTER
    key = str(chat_id)
    chat_data = _ROSTER.get(key, {})
    uid = str(user_id)
    if uid in chat_data:
        del chat_data[uid]
        _ROSTER[key] = chat_data
        _DIRTY_ROSTER = True


def _get_members(chat_id: int) -> List[dict]:
    data = _ROSTER.get(str(chat_id), {})
    out = []
    for uid_str, info in data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        out.append({
            "id": uid,
            "first_name": (info.get("name") or "usuari").strip(),
            "username": info.get("username") or "",
            "is_bot": bool(info.get("is_bot", False)),
        })
    return out


def _mentions_chunks(members: List[dict], batch_size: int = 20) -> List[str]:
    people = [m for m in members if m.get("id") and not m.get("is_bot")]
    seen, acc, chunks = set(), [], []
    for m in people:
        uid = m["id"]
        if uid in seen:
            continue
        seen.add(uid)
        name = (m.get("first_name") or m.get("username") or "usuari").strip()
        acc.append(f'<a href="tg://user?id={uid}">{html.escape(name)}</a>')
        if len(acc) == batch_size:
            chunks.append(", ".join(acc)); acc = []
    if acc:
        chunks.append(", ".join(acc))
    return chunks

# =========================
# HANDLERS BÀSICS
# =========================

_last_all: Dict[int, float] = {}

async def _can_use_all(chat_id: int) -> bool:
    now = time.time()
    last = _last_all.get(chat_id, 0.0)
    if now - last < ALL_COOLDOWN:
        return False
    _last_all[chat_id] = now
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot en marxa. Usa /all o escriu @all …")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/all [text] — menciona tothom en lots. /roster_count — compte d'usuaris.")

async def roster_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    data = _ROSTER.get(str(chat.id), {})
    await update.message.reply_text(f"Usuaris al roster: {len(data)}")

async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await _can_use_all(chat.id):
        await update.message.reply_text("Has d'esperar abans de tornar a usar @all.")
        return
    text = " ".join(context.args) if context.args else ""
    header = f"@all per {html.escape(user.first_name)}" + (f": {html.escape(text)}" if text else "")
    members = _get_members(chat.id)
    if not members:
        await update.message.reply_text("No tinc cap llista d'usuaris per mencionar aquí.")
        return
    await update.message.reply_html(header)
    for chunk in _mentions_chunks(members):
        await update.message.reply_html(chunk, disable_web_page_preview=True)

async def trigger_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not (msg.text or msg.caption):
        return
    raw = (msg.text or msg.caption).strip()
    if not raw.lower().startswith("@all"):
        return
    args = raw.split()[1:]
    context.args = args
    await cmd_all(update, context)

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat and user and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        _upsert_member(chat.id, user)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    for u in (update.message.new_chat_members or []):
        _upsert_member(chat.id, u)

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.message.left_chat_member
    if chat and u:
        _remove_member(chat.id, u.id)

# =========================
# MAIN
# =========================

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()
    # Guardat diferit cada 15s
    app.job_queue.run_repeating(_autosave, interval=15, first=15)
    # Importació única (si s'escau)
    ensure_import_once()

    # Comandes
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["all", "todos"], cmd_all))
    app.add_handler(CommandHandler("roster_count", roster_count))

    # Trigger @all
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), trigger_all))

    # Roster auto
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), on_any_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    return app


def main():
    app = build_app()
    log.info("Bot (parxe mínim) en execució… Persistència a: %s", BASE_DIR)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
