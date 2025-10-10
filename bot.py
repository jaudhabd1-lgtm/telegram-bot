from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime
from flask import Flask
from threading import Thread
import pytz
import os

# ====== KEEP ALIVE (Replit) ======
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "I'm alive"

def _run():
    # Replit sol usar el port 8080; si no existeix, fem servir 8080 igualment
    port = int(os.getenv("PORT", "8080"))
    print(f"[keep_alive] Engegant Flask a {port}…")
    app_web.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=_run, daemon=True)
    t.start()
# ====== FI KEEP ALIVE ======

# Token via variable d'entorn (NO el posis en clar)
TOKEN = os.getenv("TOKEN")

# Geocodificador + zona horària
geolocator = Nominatim(user_agent="telegram-timebot")
tf = TimezoneFinder()

def flag_for(country_code: str) -> str:
    """Converteix codi ISO-3166-1 (ES, MX, JP...) a emoji de bandera."""
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    cc = country_code.upper()
    return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in cc)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot horario.\n\n"
        "Escribe:\n"
        "• hora españa\n"
        "• hora japón\n"
        "• hora venezuela\n\n"
        "Y te diré la hora actual con su banderita."
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if not text.startswith("hora"):
        return

    lugar = text.replace("hora", "").strip()
    if not lugar:
        await update.message.reply_text("Dime el país o ciudad, por ejemplo: «hora méxico» o «hora italia».")
        return

    try:
        location = geolocator.geocode(lugar, language="es")
        if not location:
            await update.message.reply_text(f"No pude encontrar «{lugar}». 😕")
            return

        timezone_str = tf.timezone_at(lat=location.latitude, lng=location.longitude)
        if not timezone_str:
            await update.message.reply_text(f"No pude determinar la zona horaria de «{lugar}». 😕")
            return

        zona = pytz.timezone(timezone_str)
        ahora = datetime.now(zona)
        hora_formateada = ahora.strftime("%H:%M:%S")

        bandera = ""
        if hasattr(location, 'raw') and "country_code" in location.raw:
            bandera = flag_for(location.raw["country_code"])

        respuesta = f"{bandera} Hora actual en {lugar.title()}: {hora_formateada}"
        await update.message.reply_text(respuesta)

    except Exception:
        await update.message.reply_text(f"No pude obtener la hora de «{lugar}». 😕")

def main():
    if not TOKEN:
        raise RuntimeError("Falta la variable de entorno TOKEN (posa-la a Secrets/Environment)")
    # Mantén viu el Repl per UptimeRobot
    keep_alive()
    # Arrenca el bot
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    print("[bot] Iniciant polling…")
    application.run_polling()

if __name__ == "__main__":
    main()
