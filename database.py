"""
Base de datos SQLite para el bot de tránsito.
Gestiona lotes, turnos e historial.
"""

import sqlite3
import uuid
from pathlib import Path
from typing import List, Dict, Optional

_PERSISTENT = Path("/data")
DB_PATH = _PERSISTENT / "transito.db" if _PERSISTENT.exists() else Path(__file__).parent / "transito.db"


class Database:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turnos (
                    id                  TEXT PRIMARY KEY,
                    user_id             INTEGER NOT NULL,
                    abierto             INTEGER DEFAULT 1,
                    creado_en           TEXT NOT NULL,
                    cerrado_en          TEXT,
                    canastas_estimadas  INTEGER
                )
            """)
            # Migración: agregar columna si no existe (DBs anteriores)
            try:
                conn.execute("ALTER TABLE turnos ADD COLUMN canastas_estimadas INTEGER")
                conn.commit()
            except Exception:
                pass  # columna ya existe
            try:
                conn.execute("ALTER TABLE turnos ADD COLUMN proyeccion_items TEXT")
                conn.commit()
            except Exception:
                pass  # columna ya existe
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lotes (
                    id                TEXT PRIMARY KEY,
                    turno_id          TEXT NOT NULL,
                    user_id           INTEGER NOT NULL,
                    maquina           TEXT NOT NULL,
                    canastas          INTEGER NOT NULL,
                    presentacion      TEXT NOT NULL,
                    presentacion_raw  TEXT NOT NULL,
                    producto          TEXT NOT NULL,
                    producto_legible  TEXT NOT NULL,
                    pin               TEXT NOT NULL,
                    pin_legible       TEXT NOT NULL,
                    mercado           TEXT NOT NULL,
                    mercado_legible   TEXT NOT NULL,
                    cajas_por_canasta REAL NOT NULL,
                    cajas_en_transito REAL NOT NULL,
                    timestamp         TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lotes_turno ON lotes(turno_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lotes_user  ON lotes(user_id, timestamp)")
            conn.commit()

    # ── Turnos ────────────────────────────────────────────────────────────────
    def _get_o_crear_turno(self, user_id: int, timestamp: str) -> str:
        """Devuelve el turno abierto o crea uno nuevo."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM turnos WHERE user_id=? AND abierto=1 ORDER BY creado_en DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row:
                return row["id"]
            turno_id = str(uuid.uuid4())[:8].upper()
            conn.execute(
                "INSERT INTO turnos (id, user_id, creado_en) VALUES (?,?,?)",
                (turno_id, user_id, timestamp)
            )
            conn.commit()
            return turno_id

    def cerrar_turno(self, user_id: int, timestamp: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE turnos SET abierto=0, cerrado_en=? WHERE user_id=? AND abierto=1",
                (timestamp, user_id)
            )
            conn.commit()

    # ── Lotes ─────────────────────────────────────────────────────────────────
    def guardar_lote(self, user_id: int, datos: dict, timestamp: str) -> str:
        turno_id = self._get_o_crear_turno(user_id, timestamp)
        lote_id  = str(uuid.uuid4())[:8].upper()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO lotes
                  (id, turno_id, user_id, maquina, canastas, presentacion,
                   presentacion_raw, producto, producto_legible, pin, pin_legible,
                   mercado, mercado_legible, cajas_por_canasta, cajas_en_transito, timestamp)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lote_id, turno_id, user_id,
                datos["maquina"], datos["canastas"],
                datos["presentacion"], datos["presentacion_raw"],
                datos["producto"], datos["producto_legible"],
                datos["pin"], datos["pin_legible"],
                datos["mercado"], datos["mercado_legible"],
                datos["cajas_por_canasta"], datos["cajas_en_transito"],
                timestamp
            ))
            conn.commit()
        return lote_id

    def eliminar_lote(self, lote_id: str, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM lotes WHERE id=? AND user_id=?",
                (lote_id, user_id)
            )
            conn.commit()
            return cur.rowcount > 0

    # ── Consultas ─────────────────────────────────────────────────────────────
    def get_lotes_turno_activo(self, user_id: int) -> List[Dict]:
        with self._conn() as conn:
            turno = conn.execute(
                "SELECT id FROM turnos WHERE user_id=? AND abierto=1 ORDER BY creado_en DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if not turno:
                return []
            rows = conn.execute(
                "SELECT * FROM lotes WHERE turno_id=? ORDER BY timestamp ASC",
                (turno["id"],)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_lotes_por_fecha(self, user_id: int, fecha: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM lotes WHERE user_id=?
                   AND SUBSTR(timestamp, 1, 10) = ?
                   ORDER BY timestamp ASC""",
                (user_id, fecha)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Proyección ────────────────────────────────────────────────────────────
    def guardar_proyeccion(self, user_id: int, canastas_estimadas: int):
        """Guarda la estimación de canastas adicionales en el turno activo."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE turnos SET canastas_estimadas=? WHERE user_id=? AND abierto=1",
                (canastas_estimadas, user_id)
            )
            conn.commit()

    def get_proyeccion(self, user_id: int) -> Optional[int]:
        """Devuelve las canastas_estimadas del turno activo, o None si no hay estimación."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT canastas_estimadas FROM turnos WHERE user_id=? AND abierto=1 ORDER BY creado_en DESC LIMIT 1",
                (user_id,)
            ).fetchone()
        return row["canastas_estimadas"] if row else None

    def guardar_proyeccion_items(self, user_id: int, items: list):
        """Guarda los items de proyección (lista de dicts) como JSON en el turno activo."""
        import json
        with self._conn() as conn:
            conn.execute(
                "UPDATE turnos SET proyeccion_items=? WHERE user_id=? AND abierto=1",
                (json.dumps(items), user_id)
            )
            conn.commit()

    def get_proyeccion_items(self, user_id: int) -> Optional[list]:
        """Devuelve los items de proyección del turno activo, o None."""
        import json
        with self._conn() as conn:
            row = conn.execute(
                "SELECT proyeccion_items FROM turnos WHERE user_id=? AND abierto=1 ORDER BY creado_en DESC LIMIT 1",
                (user_id,)
            ).fetchone()
        if row and row["proyeccion_items"]:
            return json.loads(row["proyeccion_items"])
        return None

    def get_lotes_rango(self, user_id: int, desde: str, hasta: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM lotes
                   WHERE user_id=? AND SUBSTR(timestamp, 1, 10) BETWEEN ? AND ?
                   ORDER BY timestamp ASC""",
                (user_id, desde, hasta)
            ).fetchall()
        return [dict(r) for r in rows]
