from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime
import pytz
import os

# Agafa el token de les variables d'entorn (no el posis en clar al codi)
TOKEN = os.getenv("TOKEN")

geolocator = Nominatim(user_agent="telegram-bot")
tf = TimezoneFinder()

def flag_for(country_code: str) -> str:
    """Converteix codi ISO-3166-1 alpha-2 (ES, MX, JP...) a emoji de bandera."""
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
        raise RuntimeError("Falta la variable de entorno TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()

if __name__ == "__main__":
    main()
