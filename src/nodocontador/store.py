from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


def now() -> datetime:
    return datetime.now().astimezone()


def minute_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


class CounterStore:
    """Registro local de producción en SQLite.

    Se guarda **por minuto**, no por pieza: una línea a 60 piezas por minuto
    serían 86 400 filas diarias y otras tantas escrituras sobre la tarjeta SD
    de la Zero W. Por minuto son 1 440 filas al día y cada fila acumula lo que
    pasó en ese minuto.

    La central recibe valores absolutos por minuto (no incrementos), así que
    reenviar un minuto ya entregado es inofensivo: lo pendiente es simplemente
    donde ``unidades`` difiere de ``unidades_entregadas``.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;"
            )
            self._create_schema()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS estado (
              clave TEXT PRIMARY KEY,
              valor TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turno (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              inicio TEXT NOT NULL,
              fin TEXT NULL,
              unidades INTEGER NOT NULL DEFAULT 0,
              pulsos INTEGER NOT NULL DEFAULT 0,
              motivo_cierre TEXT NULL
            );
            CREATE TABLE IF NOT EXISTS conteo_minuto (
              minuto TEXT NOT NULL,
              turno_id INTEGER NOT NULL,
              unidades INTEGER NOT NULL,
              pulsos INTEGER NOT NULL,
              unidades_entregadas INTEGER NOT NULL DEFAULT -1,
              -- Un reinicio de turno a mitad de minuto deja ese minuto
              -- repartido entre los dos turnos, cada parte en su fila.
              PRIMARY KEY (minuto, turno_id)
            );
            CREATE INDEX IF NOT EXISTS ix_minuto_pendiente
              ON conteo_minuto(minuto) WHERE unidades <> unidades_entregadas;
            CREATE TABLE IF NOT EXISTS parada (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              turno_id INTEGER NOT NULL,
              desde TEXT NOT NULL,
              hasta TEXT NULL,
              segundos INTEGER NULL
            );
            """
        )
        self._db.commit()

    # -- estado clave/valor ----------------------------------------------
    def read_state(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT valor FROM estado WHERE clave = ?", (key,)).fetchone()
            return str(row[0]) if row else None

    def write_state(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO estado(clave, valor) VALUES (?, ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
                (key, value),
            )
            self._db.commit()

    @property
    def total_units(self) -> int:
        value = self.read_state("total_unidades")
        return int(value) if value else 0

    # -- turnos -------------------------------------------------------------
    def current_shift(self) -> dict:
        """Turno abierto; si no hay ninguno se abre uno."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM turno WHERE fin IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                self._db.execute("INSERT INTO turno(inicio) VALUES (?)", (now().isoformat(),))
                self._db.commit()
                row = self._db.execute(
                    "SELECT * FROM turno WHERE fin IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return self._shift(row)

    def close_shift(self, reason: str, at: datetime | None = None) -> dict:
        """Cierra el turno abierto y abre el siguiente en el mismo instante."""
        moment = (at or now()).isoformat()
        with self._lock:
            current = self.current_shift()
            self._db.execute(
                "UPDATE turno SET fin = ?, motivo_cierre = ? WHERE id = ?",
                (moment, reason, current["id"]),
            )
            # Una parada abierta no pasa al turno nuevo: se corta aquí.
            self._close_stops(moment)
            self._db.execute("INSERT INTO turno(inicio) VALUES (?)", (moment,))
            self._db.commit()
        return self.current_shift()

    def recent_shifts(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM turno ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._shift(row) for row in rows]

    @staticmethod
    def _shift(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "inicio": row["inicio"], "fin": row["fin"],
            "unidades": row["unidades"], "pulsos": row["pulsos"],
            "motivoCierre": row["motivo_cierre"],
        }

    # -- conteo -------------------------------------------------------------
    def add_counts(self, minutes: dict[str, tuple[int, int]]) -> None:
        """Suma ``{minuto: (unidades, pulsos)}`` al turno abierto en una transacción."""
        if not minutes:
            return
        with self._lock:
            shift = self.current_shift()
            units_total = sum(units for units, _ in minutes.values())
            pulses_total = sum(pulses for _, pulses in minutes.values())
            for minute, (units, pulses) in minutes.items():
                self._db.execute(
                    "INSERT INTO conteo_minuto(minuto, turno_id, unidades, pulsos) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(minuto, turno_id) DO UPDATE SET unidades = unidades + excluded.unidades, "
                    "pulsos = pulsos + excluded.pulsos",
                    (minute, shift["id"], units, pulses),
                )
            self._db.execute(
                "UPDATE turno SET unidades = unidades + ?, pulsos = pulsos + ? WHERE id = ?",
                (units_total, pulses_total, shift["id"]),
            )
            self._db.execute(
                "INSERT INTO estado(clave, valor) VALUES ('total_unidades', ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor = CAST(valor AS INTEGER) + ?",
                (str(units_total), units_total),
            )
            self._db.commit()

    def by_hour(self, hours: int = 24) -> list[dict]:
        since = minute_key(now() - timedelta(hours=hours))
        with self._lock:
            rows = self._db.execute(
                "SELECT substr(minuto, 1, 13) AS hora, SUM(unidades) AS unidades, "
                "SUM(pulsos) AS pulsos, COUNT(DISTINCT minuto) AS minutos "
                "FROM conteo_minuto WHERE minuto >= ? GROUP BY hora ORDER BY hora",
                (since,),
            ).fetchall()
        return [
            {"hora": row["hora"] + ":00", "unidades": row["unidades"],
             "pulsos": row["pulsos"], "minutosConProduccion": row["minutos"]}
            for row in rows
        ]

    def minutes_since(self, since: str, limit: int = 1440) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT minuto, turno_id, unidades, pulsos FROM conteo_minuto "
                "WHERE minuto >= ? ORDER BY minuto LIMIT ?",
                (since, limit),
            ).fetchall()
        return [
            {"minuto": row["minuto"], "turnoId": row["turno_id"],
             "unidades": row["unidades"], "pulsos": row["pulsos"]}
            for row in rows
        ]

    # -- entrega a la central ------------------------------------------------
    def pending_minutes(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT minuto, turno_id, unidades, pulsos FROM conteo_minuto "
                "WHERE unidades <> unidades_entregadas ORDER BY minuto LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"minuto": row["minuto"], "turnoId": row["turno_id"],
             "unidades": row["unidades"], "pulsos": row["pulsos"]}
            for row in rows
        ]

    @property
    def pending_count(self) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM conteo_minuto WHERE unidades <> unidades_entregadas"
            ).fetchone()[0])

    def mark_delivered(self, minutes: list[dict]) -> None:
        """Marca lo que la central aceptó, con el valor que se le mandó.

        Si el minuto siguió sumando mientras la petición viajaba, queda
        pendiente de nuevo con el valor mayor.
        """
        with self._lock:
            self._db.executemany(
                "UPDATE conteo_minuto SET unidades_entregadas = ? WHERE minuto = ? AND turno_id = ?",
                [(item["unidades"], item["minuto"], item["turnoId"]) for item in minutes],
            )
            self._db.commit()

    # -- paradas ------------------------------------------------------------
    def open_stop(self, since: datetime) -> None:
        with self._lock:
            shift = self.current_shift()
            if self._db.execute("SELECT 1 FROM parada WHERE hasta IS NULL").fetchone():
                return
            self._db.execute(
                "INSERT INTO parada(turno_id, desde) VALUES (?, ?)", (shift["id"], since.isoformat())
            )
            self._db.commit()

    def close_stop(self, until: datetime) -> None:
        with self._lock:
            self._close_stops(until.isoformat())
            self._db.commit()

    def _close_stops(self, until: str) -> None:
        end = datetime.fromisoformat(until)
        for row in self._db.execute("SELECT id, desde FROM parada WHERE hasta IS NULL").fetchall():
            seconds = max(0, int((end - datetime.fromisoformat(row["desde"])).total_seconds()))
            self._db.execute(
                "UPDATE parada SET hasta = ?, segundos = ? WHERE id = ?", (until, seconds, row["id"])
            )

    def recent_stops(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM parada ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": row["id"], "turnoId": row["turno_id"], "desde": row["desde"],
             "hasta": row["hasta"], "segundos": row["segundos"]}
            for row in rows
        ]

    def stop_seconds_in_shift(self, shift_id: int) -> int:
        with self._lock:
            value = self._db.execute(
                "SELECT COALESCE(SUM(segundos), 0) FROM parada WHERE turno_id = ? AND hasta IS NOT NULL",
                (shift_id,),
            ).fetchone()[0]
        return int(value)

    def close(self) -> None:
        with self._lock:
            self._db.close()
