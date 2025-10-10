from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime
import pytz
import emoji_flags
import os

TOKEN = os.getenv("TOKEN")

geolocator = Nominatim(user_agent="telegram-bot")
tf = TimezoneFinder()

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if not text.startswith("hora"):
        return

    lugar = text.replace("hora", "").strip()
    if not lugar:
        await update.message.reply_text("Dime el país o ciudad, por ejemplo: «hora japón» o «hora méxico».")
        return

    try:
        location = geolocator.geocode(lugar, language="es")
        if not location:
            await update.message.reply_text(f"No he podido encontrar «{lugar}». 😕")
            return

        timezone_str = tf.timezone_at(lat=location.latitude, lng=location.longitude)
        if not timezone_str:
            await update.message.reply_text(f"No he podido determinar la zona horaria de «{lugar}». 😕")
            return

        zona = pytz.timezone(timezone_str)
        ahora = datetime.now(zona)
        hora_formateada = ahora.strftime("%H:%M:%S")

        bandera = ""
        if hasattr(location, 'raw') and "country_code" in location.raw:
            codigo = location.raw["country_code"].upper()
            bandera = emoji_flags.get_flag(codigo) or ""

        respuesta = f"{bandera} Hora actual en {lugar.title()}: {hora_formateada}"
        await update.message.reply_text(respuesta)

    except Exception as e:
        await update.message.reply_text(f"No pude obtener la hora de «{lugar}». 😕")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()

if __name__ == "__main__":
    main()
