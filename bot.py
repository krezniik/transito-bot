"""
Bot de Telegram — Calculadora de Cajas en Tránsito
====================================================
Operación completa desde Telegram: ingreso de lotes, cálculo,
recordatorios, historial y exportación a Excel.
"""
import os
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import asyncio
import tempfile
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler, ApplicationHandlerStop
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import Database
from lote_parser import parsear_lote_con_claude, calcular_cajas, parsear_proyeccion_con_claude, TABLA
from reporter import generar_resumen_texto, enviar_reporte_grupo, exportar_excel, LINEA_MAQUINA, siguiente_hora_reporte
from recordatorio import programar_recordatorio, cancelar_recordatorio_activo
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# -- Configuración -------------------------------------------------------------
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
CHAT_ID_GRUPO     = os.getenv("CHAT_ID_GRUPO")          # Grupo donde va el reporte
ALLOWED_USERS     = set(int(u.strip()) for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()) if os.getenv("ALLOWED_USERS") else set()
TIMEZONE          = os.getenv("TIMEZONE", "America/Guatemala")
TZ                = ZoneInfo(TIMEZONE)
db        = Database()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
_bot_pausado: bool = db.get_config("pausado") == "1"
import anthropic, openai
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client    = openai.OpenAI(api_key=OPENAI_API_KEY)
# -- Helpers -------------------------------------------------------------------
def _keyboard_pin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Pin pequeño", callback_data="pin:p"),
        InlineKeyboardButton("Pin grande",  callback_data="pin:g"),
    ]])

def _keyboard_pin_proy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Pin pequeño", callback_data="pin_proy:p"),
        InlineKeyboardButton("Pin grande",  callback_data="pin_proy:g"),
    ]])

def _nombre_maquina(maquina: str) -> str:
    linea = LINEA_MAQUINA.get(maquina, "")
    return f"{maquina} ({linea})" if linea else maquina

def _texto_confirmacion_lote(d: dict, lote_id: str) -> str:
    return (
        f"✅ *Lote registrado* — ID `{lote_id}`\n\n"
        f"⚙️ {_nombre_maquina(d['maquina'])}\n"
        f"• Canastas: {d['canastas']}\n"
        f"• Cajas/canasta: {d['cajas_por_canasta']}\n"
        f"• Presentación: {d['presentacion']}\n"
        f"• Producto: {d['producto_legible']}\n"
        f"• Pin: {d['pin_legible']}\n"
        f"• Mercado: {d['mercado_legible']}\n"
        f"• *Cajas en tránsito: {int(d['cajas_en_transito']):,}* 📦\n\n"
        "Usa /resumen para ver el total del turno."
    )

# -- Seguridad -----------------------------------------------------------------
def autorizado(update: Update) -> bool:
    if not ALLOWED_USERS:
        return False
    return update.effective_user.id in ALLOWED_USERS
# -- Interceptor de pausa (group=-1, corre antes que todos los handlers) -------
async def paused_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _bot_pausado:
        return
    # /start siempre pasa — es el comando para reactivar
    if update.message and (update.message.text or "").startswith("/start"):
        return
    if update.message:
        await update.message.reply_text(
            "⏸ *Bot pausado.* No hay producción hoy.\nUsa /start para reactivar.",
            parse_mode=ParseMode.MARKDOWN
        )
    raise ApplicationHandlerStop

# -- /stop ---------------------------------------------------------------------
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _bot_pausado
    if not autorizado(update): return
    _bot_pausado = True
    db.set_config("pausado", "1")
    await update.message.reply_text(
        "⏸ *Bot pausado.* Los comandos y reportes automáticos están desactivados.\n"
        "Usa /start cuando quieras reanudar.",
        parse_mode=ParseMode.MARKDOWN
    )

# -- /start --------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _bot_pausado
    if not autorizado(update): return
    if _bot_pausado:
        _bot_pausado = False
        db.set_config("pausado", "0")
        await update.message.reply_text(
            "▶️ *Bot reactivado.* ¡Listo para registrar lotes!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    texto = (
        "👋 *Bot de Tránsito* listo.\n\n"
        "*Comandos principales:*\n"
        "`/lote` — Registrar un lote\n"
        "`/lotes` — Registrar varios lotes a la vez\n"
        "`/resumen` — Ver tránsito del turno actual\n"
        "`/enviar` — Enviar resumen al grupo\n"
        "`/reporte` — Consultar historial\n"
        "`/exportar` — Descargar Excel\n"
        "`/recordatorio` — Activar alertas periódicas\n"
        "`/cancelar_alerta` — Desactivar alertas\n"
        "`/proyeccion` — Estimar canastas hasta el cierre\n"
        "`/ayuda` — Ver guía completa\n"
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
# -- /ayuda --------------------------------------------------------------------
async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    texto = (
        "📖 *Guía de uso*\n\n"
        "*Registrar un lote:*\n"
        "`/lote Mespack1 canastas:12 pres:8 producto:N pin:P mercado:L`\n"
        "También puedes enviar una *nota de voz* describiendo el lote.\n\n"
        "*Registrar varios lotes a la vez:*\n"
        "`/lotes` y luego una línea por lote:\n"
        "`m1 canastas:12 pres:8 producto:N pin:P mercado:L`\n"
        "`m2 canastas:8 pres:14 producto:R pin:G mercado:E`\n\n"
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
        "`/cancelar_alerta` — Desactivar\n\n"
        "*Proyección:*\n"
        "`/proyeccion 80` — Estima 80 canastas adicionales hasta el cierre de turno\n"
        "Aparece en el /resumen como _Proyectado a HH:MM_\n"
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
# -- /lotes --------------------------------------------------------------------
async def cmd_lotes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)

    texto_completo = update.message.text or ""
    lineas = [l.strip() for l in texto_completo.split("\n")[1:] if l.strip()]

    if not lineas:
        await update.message.reply_text(
            "⚠️ Escribe `/lotes` y en las siguientes líneas un lote por línea:\n\n"
            "`m1 canastas:12 pres:8 producto:N pin:P mercado:L`\n"
            "`m2 canastas:8 pres:14 producto:R pin:G mercado:E`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.effective_chat.send_action("typing")

    exitosos  = []
    pendientes = []
    fallidos  = []
    for linea in lineas:
        resultado = await parsear_lote_con_claude(anthropic_client, linea, now)
        if resultado.get("error"):
            fallidos.append({"linea": linea, "error": resultado["error"]})
        elif resultado.get("requiere_confirmacion_pin"):
            pendientes.append({"datos": resultado, "timestamp": now.isoformat(), "user_id": user_id, "linea": linea})
        else:
            lote_id = db.guardar_lote(user_id=user_id, datos=resultado, timestamp=now.isoformat())
            exitosos.append({"lote_id": lote_id, "datos": resultado})

    if pendientes:
        context.user_data["cola_pin"]      = pendientes
        context.user_data["lotes_resumen"] = exitosos
        context.user_data["lotes_fallidos"] = fallidos
        primero = pendientes[0]["datos"]
        resumen_previo = ""
        if exitosos:
            resumen_previo = "".join(
                f"✅ `{r['lote_id']}` — {r['datos']['maquina']} · *{int(r['datos']['cajas_en_transito']):,} cajas*\n"
                for r in exitosos
            ) + "\n"
        await update.message.reply_text(
            resumen_previo +
            f"⚙️ *{primero['maquina']}* · {primero['canastas']} canastas · {primero['presentacion']} · {primero['producto_legible']}\n"
            "¿Qué pin usa este lote?",
            reply_markup=_keyboard_pin(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    total = len(exitosos) + len(fallidos)
    respuesta = f"📦 *{len(exitosos)}/{total} lotes registrados*\n\n"
    for r in exitosos:
        d = r["datos"]
        respuesta += (
            f"✅ `{r['lote_id']}` — {d['maquina']} · {d['canastas']} canastas · "
            f"{d['presentacion']} · {d['producto_legible']} · *{int(d['cajas_en_transito']):,} cajas*\n"
        )
    if fallidos:
        respuesta += "\n*No se pudieron registrar:*\n"
        for r in fallidos:
            respuesta += f"❌ `{r['linea']}` — {r['error']}\n"
    respuesta += "\nUsa /resumen para ver el total del turno."
    await update.message.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)

# -- /lote ---------------------------------------------------------------------
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
    if resultado.get("requiere_confirmacion_pin"):
        context.user_data["cola_pin"] = [{"datos": resultado, "timestamp": now.isoformat(), "user_id": user_id}]
        d = resultado
        await update.message.reply_text(
            f"⚙️ *{d['maquina']}* · {d['canastas']} canastas · {d['presentacion']} · {d['producto_legible']}\n"
            "¿Qué pin usa este lote?",
            reply_markup=_keyboard_pin(),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    lote_id = db.guardar_lote(user_id=user_id, datos=resultado, timestamp=now.isoformat())
    await update.message.reply_text(
        _texto_confirmacion_lote(resultado, lote_id),
        parse_mode=ParseMode.MARKDOWN
    )
# -- Confirmación de pin (inline keyboard) ------------------------------------
async def confirmar_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pin  = query.data.split(":")[1]  # "p" o "g"
    cola = context.user_data.get("cola_pin", [])
    if not cola:
        await query.edit_message_text("❌ No hay lote pendiente de confirmación.")
        return

    pendiente = cola.pop(0)
    d = pendiente["datos"]
    d["pin"]             = pin
    d["pin_legible"]     = "Grande" if pin == "g" else "Pequeño"
    d["cajas_por_canasta"] = calcular_cajas(d["producto"], d["presentacion_raw"], pin)
    d["cajas_en_transito"] = d["canastas"] * d["cajas_por_canasta"]
    lote_id = db.guardar_lote(user_id=pendiente["user_id"], datos=d, timestamp=pendiente["timestamp"])

    if cola:
        # Quedan más lotes pendientes de pin
        context.user_data["cola_pin"] = cola
        if "lotes_resumen" in context.user_data:
            context.user_data["lotes_resumen"].append({"lote_id": lote_id, "datos": d})
        siguiente = cola[0]["datos"]
        await query.edit_message_text(
            f"✅ `{lote_id}` guardado — *{int(d['cajas_en_transito']):,} cajas*\n\n"
            f"⚙️ *{siguiente['maquina']}* · {siguiente['canastas']} canastas · "
            f"{siguiente['presentacion']} · {siguiente['producto_legible']}\n"
            "¿Qué pin usa este lote?",
            reply_markup=_keyboard_pin(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # No quedan pendientes — armar respuesta final
    if "lotes_resumen" in context.user_data:
        # Flujo /lotes
        todos   = context.user_data.pop("lotes_resumen") + [{"lote_id": lote_id, "datos": d}]
        fallidos = context.user_data.pop("lotes_fallidos", [])
        context.user_data.pop("cola_pin", None)
        total    = len(todos) + len(fallidos)
        respuesta = f"📦 *{len(todos)}/{total} lotes registrados*\n\n"
        for r in todos:
            rd = r["datos"]
            respuesta += (
                f"✅ `{r['lote_id']}` — {rd['maquina']} · {rd['canastas']} canastas · "
                f"{rd['presentacion']} · {rd['producto_legible']} · *{int(rd['cajas_en_transito']):,} cajas*\n"
            )
        if fallidos:
            respuesta += "\n*No se pudieron registrar:*\n"
            for r in fallidos:
                respuesta += f"❌ `{r['linea']}` — {r['error']}\n"
        respuesta += "\nUsa /resumen para ver el total del turno."
    else:
        # Flujo /lote individual
        context.user_data.pop("cola_pin", None)
        respuesta = _texto_confirmacion_lote(d, lote_id)

    await query.edit_message_text(respuesta, parse_mode=ParseMode.MARKDOWN)

# -- Nota de voz ---------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
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
        f"⚙️ {_nombre_maquina(resultado['maquina'])}\n"
        f"• Canastas: {resultado['canastas']} × {resultado['cajas_por_canasta']} = *{int(cajas):,} cajas* 📦\n"
        f"• {resultado['presentacion']} | {resultado['producto_legible']} | {resultado['mercado_legible']}\n\n"
        "Usa /resumen para ver el total del turno."
    )
    await update.message.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)
# -- /resumen ------------------------------------------------------------------
async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    lotes_todos    = db.get_lotes_turno_activo(user_id)
    lotes_actuales = db.get_lotes_transito_actual(user_id)
    if not lotes_todos:
        await update.message.reply_text("📭 No hay lotes registrados en el turno actual.\nUsa /lote para agregar uno.")
        return
    proy_items = db.get_proyeccion_items(user_id)
    hora_proy  = siguiente_hora_reporte(now) if proy_items else None
    texto = generar_resumen_texto(
        lotes_actuales,
        hora_proyeccion=hora_proy,
        proyeccion_items=proy_items,
        lotes_acumulados=lotes_todos,
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
# -- /enviar -------------------------------------------------------------------
async def cmd_enviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    lotes   = db.get_lotes_transito_actual(user_id)
    if not lotes:
        await update.message.reply_text("📭 No hay lotes para enviar.")
        return
    if not CHAT_ID_GRUPO:
        await update.message.reply_text("❌ No está configurado el CHAT_ID_GRUPO en .env")
        return
    try:
        await enviar_reporte_grupo(context.bot, CHAT_ID_GRUPO, lotes)
        db.set_transito_marcador(user_id, now.isoformat())
        await update.message.reply_text(
            "✅ Resumen enviado al grupo.\n"
            "🔄 Tránsito reiniciado — el Detalle acumulado sigue disponible en /resumen."
        )
    except Exception as e:
        logger.error(f"Error enviando al grupo: {e}")
        await update.message.reply_text(f"❌ Error al enviar: {e}")
# -- /reiniciar_transito -------------------------------------------------------
async def cmd_reiniciar_transito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    lotes   = db.get_lotes_transito_actual(user_id)
    if not lotes:
        await update.message.reply_text("ℹ️ El tránsito ya está vacío. Los lotes anteriores siguen en el Detalle.")
        return
    db.set_transito_marcador(user_id, now.isoformat())
    await update.message.reply_text(
        "🔄 *Tránsito reiniciado.*\n"
        "Los lotes anteriores quedan en el Detalle acumulado de /resumen.",
        parse_mode=ParseMode.MARKDOWN
    )

# -- /nuevo_turno --------------------------------------------------------------
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
# -- /eliminar_lote ------------------------------------------------------------
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
# -- /reporte ------------------------------------------------------------------
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    arg     = context.args[0].lower() if context.args else "hoy"
    if arg == "hoy":
        fecha = now.date()
    elif arg == "ayer":
        fecha = (now - timedelta(days=1)).date()
    else:
        try:
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
# -- /exportar -----------------------------------------------------------------
async def cmd_exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    arg     = context.args[0].lower() if context.args else "hoy"
    if arg == "hoy":
        desde = hasta = now.date()
    elif arg == "ayer":
        d = (now - timedelta(days=1)).date()
        desde = hasta = d
    elif arg == "semana":
        hasta = now.date()
        desde = hasta - timedelta(days=6)
    else:
        try:
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
    try:
        with open(ruta_excel, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"transito_{desde}_{hasta}.xlsx",
                caption=f"📊 Reporte de tránsito: {desde} -> {hasta}"
            )
    finally:
        os.unlink(ruta_excel)
# -- /recordatorio -------------------------------------------------------------
async def cmd_recordatorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⏰ Uso: `/recordatorio <horas>`\nEjemplo: `/recordatorio 2` -> alerta cada 2 horas",
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
# -- /proyeccion ---------------------------------------------------------------
def _texto_confirmacion_proyeccion(items: list, hora: str) -> str:
    lineas = [f"✅ *Proyección guardada hasta las {hora} horas:*\n"]
    for item in items:
        lineas.append(
            f"  {item['presentacion']} · Pin {item['pin_legible']} · "
            f"{item['canastas']} canastas · *{int(item['cajas']):,} cajas*"
        )
    lineas.append("\nSe mostrará en el /resumen.")
    return "\n".join(lineas)


async def cmd_proyeccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    now     = datetime.now(TZ)
    texto   = " ".join(context.args) if context.args else ""

    if not texto:
        hora = siguiente_hora_reporte(now)
        items_actuales = db.get_proyeccion_items(user_id)
        estado = ""
        if items_actuales:
            estado = "\n\nProyección actual:"
            for it in items_actuales:
                estado += f"\n  {it['presentacion']} · Pin {it['pin_legible']} · {it['canastas']} canastas"
        await update.message.reply_text(
            f"📈 Envía las canastas estimadas hasta las *{hora}*.\n"
            f"Ejemplo: `/proyeccion 10 canastas 8 onzas`"
            + estado,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.effective_chat.send_action("typing")
    resultado = await parsear_proyeccion_con_claude(anthropic_client, texto)

    if isinstance(resultado, dict) and resultado.get("error"):
        await update.message.reply_text(f"❌ {resultado['error']}")
        return

    items_ok = []
    cola = []
    for item in resultado:
        if item.get("error"):
            await update.message.reply_text(f"❌ {item['error']}")
            return
        if item.get("requiere_confirmacion_pin"):
            cola.append(item)
        else:
            items_ok.append(item)

    context.user_data["proy_confirmados"] = items_ok
    context.user_data["cola_pin_proy"]    = cola

    hora = siguiente_hora_reporte(now)
    if cola:
        siguiente = cola[0]
        await update.message.reply_text(
            f"⚙️ *{siguiente['presentacion']}* · {siguiente['canastas']} canastas\n"
            "¿Qué pin usa esta presentación?",
            reply_markup=_keyboard_pin_proy(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        db.guardar_proyeccion_items(user_id, items_ok)
        await update.message.reply_text(
            _texto_confirmacion_proyeccion(items_ok, hora),
            parse_mode=ParseMode.MARKDOWN
        )


# -- Confirmación de pin para proyección ---------------------------------------
async def confirmar_pin_proyeccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pin  = query.data.split(":")[1]
    cola = context.user_data.get("cola_pin_proy", [])
    if not cola:
        await query.edit_message_text("❌ No hay proyección pendiente de confirmación.")
        return

    item = cola.pop(0)
    item["pin"]              = pin
    item["pin_legible"]      = "Grande" if pin == "g" else "Pequeño"
    item["cajas_por_canasta"] = TABLA.get((item["presentacion_raw"], pin))
    item["cajas"]            = item["canastas"] * item["cajas_por_canasta"]

    confirmados = context.user_data.get("proy_confirmados", [])
    confirmados.append(item)
    context.user_data["proy_confirmados"] = confirmados

    if cola:
        context.user_data["cola_pin_proy"] = cola
        siguiente = cola[0]
        await query.edit_message_text(
            f"✅ {item['presentacion']} · Pin {item['pin_legible']} guardado\n\n"
            f"⚙️ *{siguiente['presentacion']}* · {siguiente['canastas']} canastas\n"
            "¿Qué pin usa esta presentación?",
            reply_markup=_keyboard_pin_proy(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Todas confirmadas — guardar
    context.user_data.pop("cola_pin_proy", None)
    context.user_data.pop("proy_confirmados", None)
    user_id = query.from_user.id
    now     = datetime.now(TZ)
    hora    = siguiente_hora_reporte(now)
    db.guardar_proyeccion_items(user_id, confirmados)
    await query.edit_message_text(
        _texto_confirmacion_proyeccion(confirmados, hora),
        parse_mode=ParseMode.MARKDOWN
    )

# -- /fecha --------------------------------------------------------------------
async def cmd_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    now = datetime.now(TZ)
    dia_juliano = now.timetuple().tm_yday
    semana      = now.isocalendar().week
    texto = (
        f"📅 *Fecha actual*\n\n"
        f"• Fecha:       {now.strftime('%d/%m/%Y')}\n"
        f"• Día juliano: {dia_juliano}\n"
        f"• Semana:      {semana}"
    )
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)

# -- /cancelar_alerta ---------------------------------------------------------
async def cmd_cancelar_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update): return
    user_id = update.effective_user.id
    cancelado = cancelar_recordatorio_activo(scheduler, user_id)
    if cancelado:
        await update.message.reply_text("🔕 Alerta periódica desactivada.")
    else:
        await update.message.reply_text("ℹ️ No tenías ninguna alerta activa.")
# -- Arranque ------------------------------------------------------------------
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start",           "Iniciar / reactivar el bot"),
        BotCommand("stop",            "Pausar el bot (días sin producción)"),
        BotCommand("lote",            "Registrar un lote"),
        BotCommand("lotes",           "Registrar varios lotes a la vez"),
        BotCommand("resumen",         "Ver tránsito del turno"),
        BotCommand("enviar",          "Enviar resumen al grupo"),
        BotCommand("reiniciar_transito", "Reiniciar tránsito sin perder historial"),
        BotCommand("nuevo_turno",     "Cerrar turno y empezar de cero"),
        BotCommand("eliminar_lote",   "Eliminar un lote por ID"),
        BotCommand("reporte",         "Consultar historial por fecha"),
        BotCommand("exportar",        "Descargar Excel"),
        BotCommand("recordatorio",    "Activar alerta periódica"),
        BotCommand("cancelar_alerta", "Desactivar alerta"),
        BotCommand("proyeccion",      "Guardar canastas estimadas al cierre"),
        BotCommand("fecha",           "Ver fecha, día juliano y semana"),
        BotCommand("ayuda",           "Guía completa"),
    ])
    scheduler.start()
    # -- Reportes automáticos por turno 
    allowed = list(ALLOWED_USERS) if ALLOWED_USERS else []
    async def reporte_automatico(bot, turno_nombre):
        if _bot_pausado:
            logger.info(f"Reporte automático omitido — bot pausado ({turno_nombre})")
            return
        now = datetime.now(TZ)
        for uid in allowed:
            try:
                lotes_todos    = db.get_lotes_turno_activo(uid)
                lotes_actuales = db.get_lotes_transito_actual(uid)
                if not lotes_todos:
                    await bot.send_message(
                        chat_id=uid,
                        text=f"⏰ *Reporte automatico - {turno_nombre}*\n\nNo hay lotes registrados en este turno.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    continue
                # Enviar resumen al usuario
                proy_items = db.get_proyeccion_items(uid)
                hora_proy  = siguiente_hora_reporte(now) if proy_items else None
                texto = f"⏰ *Reporte automatico - {turno_nombre}*\n\n" + generar_resumen_texto(
                    lotes_actuales,
                    hora_proyeccion=hora_proy,
                    proyeccion_items=proy_items,
                    lotes_acumulados=lotes_todos,
                )
                await bot.send_message(chat_id=uid, text=texto, parse_mode=ParseMode.MARKDOWN)
                # Enviar al grupo si está configurado
                if CHAT_ID_GRUPO:
                    await enviar_reporte_grupo(bot, CHAT_ID_GRUPO, lotes_actuales)
                # Cerrar turno automáticamente
                db.cerrar_turno(uid, now.isoformat())
                await bot.send_message(
                    chat_id=uid,
                    text=f"✅ *{turno_nombre} cerrado automáticamente.*\nEl siguiente registro iniciará un turno nuevo.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error en reporte automático para {uid}: {e}")
    # Turno 1 -> reporte a las 15:00
    scheduler.add_job(
        reporte_automatico,
        trigger="cron", hour=15, minute=0, timezone=TIMEZONE,
        args=[application.bot, "Turno 1 (07:00–16:00)"],
        id="reporte_turno1", replace_existing=True
    )
    # Turno 2 -> reporte a las 22:00
    scheduler.add_job(
        reporte_automatico,
        trigger="cron", hour=22, minute=0, timezone=TIMEZONE,
        args=[application.bot, "Turno 2 (16:00–23:00)"],
        id="reporte_turno2", replace_existing=True
    )
    # Turno 3 -> reporte a las 05:00
    scheduler.add_job(
        reporte_automatico,
        trigger="cron", hour=5, minute=0, timezone=TIMEZONE,
        args=[application.bot, "Turno 3 (23:00–07:00)"],
        id="reporte_turno3", replace_existing=True
    )
    logger.info("Bot iniciado ✅ — Reportes automáticos programados: 05:00, 15:00, 22:00")
def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL,        paused_interceptor), group=-1)
    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("stop",            cmd_stop))
    app.add_handler(CommandHandler("ayuda",           cmd_ayuda))
    app.add_handler(CommandHandler("lote",            cmd_lote))
    app.add_handler(CommandHandler("lotes",           cmd_lotes))
    app.add_handler(CommandHandler("resumen",         cmd_resumen))
    app.add_handler(CommandHandler("enviar",          cmd_enviar))
    app.add_handler(CommandHandler("reiniciar_transito", cmd_reiniciar_transito))
    app.add_handler(CommandHandler("nuevo_turno",     cmd_nuevo_turno))
    app.add_handler(CommandHandler("eliminar_lote",   cmd_eliminar_lote))
    app.add_handler(CommandHandler("reporte",         cmd_reporte))
    app.add_handler(CommandHandler("exportar",        cmd_exportar))
    app.add_handler(CommandHandler("recordatorio",    cmd_recordatorio))
    app.add_handler(CommandHandler("cancelar_alerta", cmd_cancelar_alerta))
    app.add_handler(CommandHandler("proyeccion",      cmd_proyeccion))
    app.add_handler(CommandHandler("fecha",           cmd_fecha))
    app.add_handler(MessageHandler(filters.VOICE,     handle_voice))
    app.add_handler(CallbackQueryHandler(confirmar_pin,          pattern="^pin:"))
    app.add_handler(CallbackQueryHandler(confirmar_pin_proyeccion, pattern="^pin_proy:"))
    logger.info("🤖 Escuchando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__ == "__main__":
    main()
