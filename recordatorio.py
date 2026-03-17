"""
Módulo de recordatorios periódicos.
Permite activar una alerta cada X horas para recordar registrar datos.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

JOB_PREFIX = "alerta_transito_"


async def _enviar_alerta(bot, user_id: int):
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⏰ *Recordatorio de tránsito*\n\n"
                "¿Ya registraste los lotes de las llenadores?\n"
                "Usa /lote para agregar o /resumen para revisar el turno actual."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error enviando alerta a {user_id}: {e}")


def programar_recordatorio(
    scheduler: AsyncIOScheduler,
    application,
    user_id: int,
    horas: int
):
    """Programa o reemplaza una alerta periódica para el usuario."""
    job_id = f"{JOB_PREFIX}{user_id}"

    # Cancelar si ya existe
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        _enviar_alerta,
        trigger=IntervalTrigger(hours=horas),
        args=[application.bot, user_id],
        id=job_id,
        replace_existing=True,
    )
    logger.info(f"Alerta periódica activada para user {user_id} cada {horas}h")


def cancelar_recordatorio_activo(scheduler: AsyncIOScheduler, user_id: int) -> bool:
    """Cancela la alerta del usuario. Devuelve True si existía."""
    job_id = f"{JOB_PREFIX}{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        return True
    return False
