# 🐸 RuruBot

RuruBot es un bot de Telegram personalizado para la gestión de grupos: incluye sistema **AFK**, **autoresponders**, menciones **@all** y **@admin**, además de un **Tres en Raya interactivo** con ranking global.

El bot está optimizado para funcionar 24/7 en **Render**, con almacenamiento persistente de datos y modo especial de **Halloween 🎃**.

---

## 🚀 Despliegue

El bot se ejecuta automáticamente a través del archivo `render.yaml`.  
Render crea un servicio tipo **Worker**, que ejecuta el comando:

```bash
python bot.py
