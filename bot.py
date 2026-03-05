"""
Bot de Telegram — Calculadora de Cajas en Tránsito
====================================================
Operación completa desde Telegram: ingreso de lotes, cálculo,
recordatorios, historial y exportación a Excel.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database
from lote_parser import parsear_lote_con_claude
from reporter import generar_resumen_texto, enviar_reporte_grupo, exportar_excel
from recordatorio import programar_recordatorio, cancelar_recordatorio_activo

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
CHAT_ID_GRUPO     = os.getenv("CHAT_ID_GRUPO")          # Grupo donde va el reporte
ALLOWED_USERS     = set(map(int, os.getenv("ALLOWED_USERS", "").split(","))) if os.getenv("ALLOWED_USERS") else set()
TIMEZONE          = os.getenv("TIMEZONE", "America/Guatemala")
TZ                = ZoneInfo(TIMEZONE)

db        = Database()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

import anthropic, openai
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client    = openai.OpenAI(api_key=OPENAI_API_KEY)


# ── Seguridad ─────────────────────────────────────────────────────────────────
def autorizado(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user.id in ALLOWED_USERS


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    texto = (
        "👋 *Bot de Tránsito* listo.\n\n"
        "*Comandos principales:*\n"
        "`/lote` — Registrar un lote\n"
        "`/resumen` — Ver tránsito del turno actual\n"
        "`/enviar` — Enviar resumen al grupo\n"
        "`/reporte` — Consultar historial\n"
        "`/exportar` — Descargar Excel\n"
        "`/recordatorio` — Activar alertas periódicas\n"
        "`/cancelar_alerta` — Desactivar alertas\n"
        "`/ayuda` — Ver guía completa\n"
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)


# ── /ayuda ────────────────────────────────────────────────────────────────────
async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    texto = (
        "📖 *Guía de uso*\n\n"
        "*Registrar un lote:*\n"
        "`/lote Mespack1 canastas:12 pres:8 producto:N pin:P mercado:L`\n"
        "También puedes enviar una *nota de voz* describiendo el lote.\n\n"
        "*Abreviaciones válidas:*\n"
        "• Máquinas: `m1` `m2` `m3` `chub`\n"
        "• Presentaciones: `4` `8` `14` `16` `28` `35` `40` `80` `4lbs`\n"
        "• Producto: `N` `R` `NE` `RE` `RS` `NA` `NP` `RP`\n"
        "• Pin: `P` (pequeño) `G` (grande)\n"
        "• Mercado: `L` (RTCA) `E` (FDA)\n\n"
        "*Resumen y envío:*\n"
        "`/resumen` — Ver cálculo del turno\n"
        "`/enviar` — Mandar al grupo de Telegram\n"
        "`/nuevo_turno` — Limpiar y empezar de cero\n\n"
        "*Historial:*\n"
        "`/reporte hoy`\n"
        "`/reporte ayer`\n"
        "`/reporte 2025-02-20`\n\n"
        "*Exportar:*\n"
        "`/exportar hoy`\n"
        "`/exportar semana`\n"
        "`/exportar 2025-02-20`\n\n"
        "*Recordatorios:*\n"
        "`/recordatorio 2` — Alerta cada 2 horas\n"
        "`/cancelar_alerta` — Desactivar\n"
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)


# ── /lote ─────────────────────────────────────────────────────────────────────
async def cmd_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)

    if not context.args:
        await update.message.reply_text(
            "⚠️ Formato: `/lote Mespack1 canastas:12 pres:8 producto:N pin:P mercado:L`\n"
            "O envíame una nota de voz con los datos.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    texto_lote = " ".join(context.args)
    await update.effective_chat.send_action("typing")

    resultado = await parsear_lote_con_claude(anthropic_client, texto_lote, now)

    if resultado.get("error"):
        await update.message.reply_text(f"❌ {resultado['error']}")
        return

    # Guardar en BD
    lote_id = db.guardar_lote(user_id=user_id, datos=resultado, timestamp=now.isoformat())

    maquina       = resultado["maquina"]
    cajas         = resultado["cajas_en_transito"]
    presentacion  = resultado["presentacion"]
    producto_leg  = resultado["producto_legible"]
    mercado_leg   = resultado["mercado_legible"]
    pin_leg       = resultado["pin_legible"]
    c_x_canasta   = resultado["cajas_por_canasta"]
    canastas      = resultado["canastas"]

    respuesta = (
        f"✅ *Lote registrado* — ID `{lote_id}`\n\n"
        f"⚙️ {maquina}\n"
        f"• Canastas: {canastas}\n"
        f"• Cajas/canasta: {c_x_canasta}\n"
        f"• Presentación: {presentacion}\n"
        f"• Producto: {producto_leg}\n"
        f"• Pin: {pin_leg}\n"
        f"• Mercado: {mercado_leg}\n"
        f"• *Cajas en tránsito: {int(cajas):,}* 📦\n\n"
        f"Usa /resumen para ver el total del turno."
    )
    await update.message.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)


# ── Nota de voz ───────────────────────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    import tempfile
    from pathlib import Path

    await update.effective_chat.send_action("typing")
    now = datetime.now(TZ)

    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await voice_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="es"
            )
        texto = transcript.text
    except Exception as e:
        logger.error(f"Error Whisper: {e}")
        await update.message.reply_text("❌ No pude transcribir el audio.")
        return
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    await update.message.reply_text(f"🎙️ *Escuché:*\n_{texto}_", parse_mode=ParseMode.MARKDOWN)

    resultado = await parsear_lote_con_claude(anthropic_client, texto, now)

    if resultado.get("error"):
        await update.message.reply_text(
            f"⚠️ No pude identificar todos los datos del lote: {resultado['error']}\n"
            "Intenta con `/lote` especificando los datos manualmente.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_id = update.effective_user.id
    lote_id = db.guardar_lote(user_id=user_id, datos=resultado, timestamp=now.isoformat())

    cajas = resultado["cajas_en_transito"]
    respuesta = (
        f"✅ *Lote registrado desde voz* — ID `{lote_id}`\n\n"
        f"⚙️ {resultado['maquina']}\n"
        f"• Canastas: {resultado['canastas']} × {resultado['cajas_por_canasta']} = *{int(cajas):,} cajas* 📦\n"
        f"• {resultado['presentacion']} | {resultado['producto_legible']} | {resultado['mercado_legible']}\n\n"
        "Usa /resumen para ver el total del turno."
    )
    await update.message.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)


# ── /resumen ──────────────────────────────────────────────────────────────────
async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    lotes   = db.get_lotes_turno_activo(user_id)

    if not lotes:
        await update.message.reply_text("📭 No hay lotes registrados en el turno actual.\nUsa /lote para agregar uno.")
        return

    texto = generar_resumen_texto(lotes)
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)


# ── /enviar ───────────────────────────────────────────────────────────────────
async def cmd_enviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    lotes   = db.get_lotes_turno_activo(user_id)

    if not lotes:
        await update.message.reply_text("📭 No hay lotes para enviar.")
        return

    if not CHAT_ID_GRUPO:
        await update.message.reply_text("❌ No está configurado el CHAT_ID_GRUPO en .env")
        return

    try:
        await enviar_reporte_grupo(context.bot, CHAT_ID_GRUPO, lotes)
        await update.message.reply_text("✅ Resumen enviado al grupo correctamente.")
    except Exception as e:
        logger.error(f"Error enviando al grupo: {e}")
        await update.message.reply_text(f"❌ Error al enviar: {e}")


# ── /nuevo_turno ──────────────────────────────────────────────────────────────
async def cmd_nuevo_turno(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    db.cerrar_turno(user_id, now.isoformat())
    await update.message.reply_text(
        "🔄 *Turno cerrado.* Los lotes anteriores quedan en el historial.\n"
        "Puedes empezar a registrar lotes para el nuevo turno.",
        parse_mode=ParseMode.MARKDOWN
    )


# ── /eliminar_lote ────────────────────────────────────────────────────────────
async def cmd_eliminar_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    if not context.args:
        await update.message.reply_text("Uso: `/eliminar_lote <ID>`", parse_mode=ParseMode.MARKDOWN)
        return
    user_id  = update.effective_user.id
    lote_id  = context.args[0]
    eliminado = db.eliminar_lote(lote_id, user_id)
    if eliminado:
        await update.message.reply_text(f"✅ Lote `{lote_id}` eliminado.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ No encontré ese lote o no te pertenece.")


# ── /reporte ──────────────────────────────────────────────────────────────────
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    arg     = context.args[0].lower() if context.args else "hoy"

    if arg == "hoy":
        fecha = now.date()
    elif arg == "ayer":
        from datetime import timedelta
        fecha = (now - timedelta(days=1)).date()
    else:
        try:
            from datetime import date
            fecha = date.fromisoformat(arg)
        except ValueError:
            await update.message.reply_text("❌ Formato de fecha inválido. Usa: `hoy`, `ayer` o `YYYY-MM-DD`", parse_mode=ParseMode.MARKDOWN)
            return

    lotes = db.get_lotes_por_fecha(user_id, str(fecha))
    if not lotes:
        await update.message.reply_text(f"📭 No hay registros para *{fecha}*.", parse_mode=ParseMode.MARKDOWN)
        return

    texto = f"📅 *Reporte del {fecha}*\n\n" + generar_resumen_texto(lotes)
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)


# ── /exportar ─────────────────────────────────────────────────────────────────
async def cmd_exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    arg     = context.args[0].lower() if context.args else "hoy"

    if arg == "hoy":
        desde = hasta = now.date()
    elif arg == "ayer":
        from datetime import timedelta
        d = (now - timedelta(days=1)).date()
        desde = hasta = d
    elif arg == "semana":
        from datetime import timedelta
        hasta = now.date()
        desde = hasta - timedelta(days=6)
    else:
        try:
            from datetime import date
            desde = hasta = date.fromisoformat(arg)
        except ValueError:
            await update.message.reply_text("❌ Usa: `hoy`, `ayer`, `semana` o `YYYY-MM-DD`", parse_mode=ParseMode.MARKDOWN)
            return

    lotes = db.get_lotes_rango(user_id, str(desde), str(hasta))
    if not lotes:
        await update.message.reply_text(f"📭 No hay registros entre {desde} y {hasta}.")
        return

    await update.effective_chat.send_action("upload_document")
    ruta_excel = exportar_excel(lotes, desde, hasta)

    with open(ruta_excel, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"transito_{desde}_{hasta}.xlsx",
            caption=f"📊 Reporte de tránsito: {desde} → {hasta}"
        )

    import os
    os.unlink(ruta_excel)


# ── /recordatorio ─────────────────────────────────────────────────────────────
async def cmd_recordatorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⏰ Uso: `/recordatorio <horas>`\nEjemplo: `/recordatorio 2` → alerta cada 2 horas",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    horas = int(context.args[0])
    if horas < 1 or horas > 24:
        await update.message.reply_text("❌ Las horas deben estar entre 1 y 24.")
        return

    programar_recordatorio(scheduler, context.application, user_id, horas)
    await update.message.reply_text(
        f"✅ Alerta activada cada *{horas} hora(s)*.\n"
        "Usa /cancelar\\_alerta para desactivarla.",
        parse_mode=ParseMode.MARKDOWN
    )


# ── /cancelar_alerta ─────────────────────────────────────────────────────────
async def cmd_cancelar_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    cancelado = cancelar_recordatorio_activo(scheduler, user_id)
    if cancelado:
        await update.message.reply_text("🔕 Alerta periódica desactivada.")
    else:
        await update.message.reply_text("ℹ️ No tenías ninguna alerta activa.")


# ── Arranque ──────────────────────────────────────────────────────────────────
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start",           "Iniciar el bot"),
        BotCommand("lote",            "Registrar un lote"),
        BotCommand("resumen",         "Ver tránsito del turno"),
        BotCommand("enviar",          "Enviar resumen al grupo"),
        BotCommand("nuevo_turno",     "Cerrar turno y empezar de cero"),
        BotCommand("eliminar_lote",   "Eliminar un lote por ID"),
        BotCommand("reporte",         "Consultar historial por fecha"),
        BotCommand("exportar",        "Descargar Excel"),
        BotCommand("recordatorio",    "Activar alerta periódica"),
        BotCommand("cancelar_alerta", "Desactivar alerta"),
        BotCommand("ayuda",           "Guía completa"),
    ])
    scheduler.start()
    logger.info("Bot iniciado ✅")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("ayuda",           cmd_ayuda))
    app.add_handler(CommandHandler("lote",            cmd_lote))
    app.add_handler(CommandHandler("resumen",         cmd_resumen))
    app.add_handler(CommandHandler("enviar",          cmd_enviar))
    app.add_handler(CommandHandler("nuevo_turno",     cmd_nuevo_turno))
    app.add_handler(CommandHandler("eliminar_lote",   cmd_eliminar_lote))
    app.add_handler(CommandHandler("reporte",         cmd_reporte))
    app.add_handler(CommandHandler("exportar",        cmd_exportar))
    app.add_handler(CommandHandler("recordatorio",    cmd_recordatorio))
    app.add_handler(CommandHandler("cancelar_alerta", cmd_cancelar_alerta))
    app.add_handler(MessageHandler(filters.VOICE,     handle_voice))

    logger.info("🤖 Escuchando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
