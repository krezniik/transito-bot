"""
Módulo de parsing de lotes con Claude.
Interpreta texto libre o transcripciones de voz y extrae los datos
del lote, calcula cajas en tránsito y devuelve un dict listo para guardar.
"""

import json
import logging
from datetime import datetime
from typing import Dict

logger = logging.getLogger(__name__)

# ── Tablas de conversión (igual que el script original) ───────────────────────
TABLA = {
    ('4',    'p'): 110,
    ('8',    'p'): 93.5,
    ('8',    'g'): 68,
    ('14',   'p'): 110,
    ('14',   'g'): 80,
    ('16',   'p'): 165,
    ('28',   'g'): 58,
    ('35',   'g'): 53,
    ('40',   'g'): 48,
    ('80',   'g'): 53,
}

TABLA_ENTEROS = {
    ('NE', '8',    'p'): 66,
    ('NE', '14',   'p'): 80,
    ('RE', '14',   'p'): 80,
    ('NE', '28',   'g'): 58,
    ('RE', '28',   'g'): 58,
    ('NE', '4lbs', 'g'): 81,
    ('RE', '4lbs', 'g'): 81,
}

# Máquinas que siempre usan pin grande
MAQUINAS_PIN_GRANDE = {'Mespack 3', 'Chub'}

MAQUINAS_VALIDAS = {
    'm1': 'Mespack 1', 'mespack1': 'Mespack 1', 'mespack 1': 'Mespack 1',
    'm2': 'Mespack 2', 'mespack2': 'Mespack 2', 'mespack 2': 'Mespack 2',
    'm3': 'Mespack 3', 'mespack3': 'Mespack 3', 'mespack 3': 'Mespack 3',
    'chub': 'Chub',
}

PRODUCTOS_LEGIBLES = {
    'N':  'FND 🫘',
    'R':  'FRD 🫘',
    'NE': 'FND Enteros 🫘',
    'RE': 'FRD Enteros 🫘',
    'RS': 'FRD Seda 🫘',
    'NA': 'FND Arreglados 🫘',
    'NP': 'FND Picante Medio 🫘',
    'RP': 'FRD Picante Medio 🫘',
}

SYSTEM_PROMPT = """Eres un asistente de planta procesadora de frijoles.
Extrae datos de lotes y responde SOLO con JSON válido, sin texto adicional.

Estructura exacta:
{"maquina_raw": "", "canastas": 0, "presentacion": "", "producto": "", "pin": "", "mercado": "", "error": null}

Conversiones OBLIGATORIAS:
- presentacion: "8oz","8 oz","ocho onzas" → "8" | "14oz" → "14" | "4lbs","cuatro libras" → "4lbs" | solo el número sin unidad
- producto: "negro","frijol negro","FND","N" → "N" | "rojo","FRD","R" → "R" | "enteros negros","fne","FNE","NE" → "NE" | "enteros rojos","fre","FRE","RE" → "RE" | "seda","RS" → "RS"
- pin: "pequeño","pequeno","chico","P" → "p" | "grande","gran","G" → "g"
- mercado: "local","RTCA","L","guatemala" → "L" | "exportacion","FDA","E","export" → "E"
- maquina_raw: "m1","mespack1","llenadora 1" → "m1" | "m2" → "m2" | "m3" → "m3" | "chub" → "chub"
- Si Mespack 3 o Chub con presentacion distinta de "8" y "14", pin siempre es "g"
- Si Mespack 3 con presentacion "8" o "14": extraer pin del texto; si no se menciona, dejar pin vacío ""
- Si falta dato, error: "Falta: <campo>"
"""


def calcular_cajas(producto: str, presentacion: str, pin: str) -> float | None:
    """Calcula cajas por canasta según las tablas de conversión."""
    if producto in ('NE', 'RE'):
        key = (producto, presentacion, pin)
        if key in TABLA_ENTEROS:
            return TABLA_ENTEROS[key]
    key = (presentacion, pin)
    return TABLA.get(key)


def humanizar(datos: dict, pin_explicito: bool = False) -> dict:
    """Agrega campos legibles y calcula totales al dict de datos del lote."""
    maquina_raw  = datos.get("maquina_raw", "").lower().strip()
    maquina      = MAQUINAS_VALIDAS.get(maquina_raw, maquina_raw.title())
    producto     = datos.get("producto", "N")
    presentacion = datos.get("presentacion", "")
    canastas     = datos.get("canastas", 0)
    mercado      = datos.get("mercado", "L")

    # Pin: forzar grande para Mespack 3 y Chub, excepto M3 con 8oz o 14oz
    pin = datos.get("pin") or "p"
    requiere_confirmacion_pin = False
    if maquina in MAQUINAS_PIN_GRANDE:
        if maquina == "Mespack 3" and presentacion in ("8", "14"):
            if not pin_explicito:
                requiere_confirmacion_pin = True
        else:
            pin = "g"

    cajas_por_canasta = calcular_cajas(producto, presentacion, pin)
    if cajas_por_canasta is None:
        return {"error": f"Combinación no válida: presentación={presentacion}, pin={pin}, producto={producto}"}

    cajas_en_transito = canastas * cajas_por_canasta

    # Presentación legible
    pres_legible = f"{presentacion} oz" if presentacion != "4lbs" else "4 lbs"

    return {
        "maquina":           maquina,
        "canastas":          canastas,
        "presentacion":      pres_legible,
        "presentacion_raw":  presentacion,
        "producto":          producto,
        "producto_legible":  PRODUCTOS_LEGIBLES.get(producto, producto),
        "pin":               pin,
        "pin_legible":       "Grande" if pin == "g" else "Pequeño",
        "mercado":           mercado,
        "mercado_legible":   "RTCA 🇬🇹" if mercado == "L" else "FDA 🇺🇸",
        "cajas_por_canasta":        cajas_por_canasta,
        "cajas_en_transito":        cajas_en_transito,
        "error":                    None,
        "requiere_confirmacion_pin": requiere_confirmacion_pin,
    }


async def parsear_lote_con_claude(client, texto: str, now: datetime) -> Dict:
    """
    Usa Claude Haiku para extraer los datos del lote desde texto libre.
    Devuelve un dict con todos los campos calculados o {"error": "..."}.
    """
    raw = ""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": texto}],
        )
        raw = response.content[0].text.strip()
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"JSON inválido de Claude: {e} | raw: {raw}")
        return {"error": "No pude interpretar los datos del lote."}
    except Exception as e:
        logger.error(f"Error Claude: {e}")
        return {"error": "Error al procesar con IA."}

    if parsed.get("error"):
        return {"error": parsed["error"]}

    pin_explicito = bool(parsed.get("pin"))
    return humanizar(parsed, pin_explicito=pin_explicito)
