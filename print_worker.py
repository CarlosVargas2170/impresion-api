import json
import logging
import os
import signal
import threading
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from app import (
    Formulario,
    nombre_impresora_efectiva,
    normalizar_printer_id,
    obtener_control_worker,
    poseer_impresora,
    poseer_printer_id,
    procesar_impresion,
    registrar_heartbeat_worker,
)


LOGGER = logging.getLogger("nexus.print_worker")
DIRECTORIO_BASE = Path(__file__).resolve().parent
RUTA_CONFIG = DIRECTORIO_BASE / "config.json"
RUTA_CONFIG_WORKER = DIRECTORIO_BASE / "worker_config.json"

CLAIM_NEXT_PERSON_SQL = """
WITH candidate AS (
    SELECT id
    FROM persons
    WHERE pending_to_print = 0
    ORDER BY print_claimed_at NULLS FIRST, created_at {order_direction}, id {order_direction}
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE persons AS person
SET pending_to_print = 1,
    print_claimed_at = NOW(),
    print_attempts = print_attempts + 1,
    print_error = NULL,
    updated_at = NOW()
FROM candidate
WHERE person.id = candidate.id
RETURNING
    person.id,
    person.first_name,
    person.paternal_surname,
    person.maternal_surname,
    person.description,
    person.company,
    person.job_title,
    person.phone_prefix,
    person.phone_number,
    person.email,
    person.consent_at
"""

MARK_PRINTED_SQL = """
UPDATE persons
SET pending_to_print = 1,
    printed_at = NOW(),
    print_error = NULL,
    updated_at = NOW()
WHERE id = %s AND pending_to_print = 1
"""

MARK_FAILED_SQL = """
UPDATE persons
SET pending_to_print = 0,
    print_error = %s,
    updated_at = NOW()
WHERE id = %s AND pending_to_print = 1
"""

SEARCH_PEOPLE_SQL = """
SELECT
    id::text AS id,
    first_name,
    paternal_surname,
    maternal_surname,
    description,
    company,
    job_title,
    phone_prefix,
    phone_number,
    email,
    consent_at,
    CASE
        WHEN pending_to_print = 1 AND printed_at IS NULL THEN 'processing'
        WHEN printed_at IS NOT NULL THEN 'printed'
        ELSE 'pending'
    END AS print_state,
    print_claimed_at,
    printed_at,
    print_error
FROM persons
WHERE is_active = TRUE
  AND (
      %s = 'all'
      OR (%s = 'pending' AND pending_to_print = 0 AND printed_at IS NULL)
      OR (%s = 'processing' AND pending_to_print = 1 AND printed_at IS NULL)
      OR (%s = 'printed' AND printed_at IS NOT NULL)
  )
  AND (
      %s = ''
      OR concat_ws(
          ' ', first_name, paternal_surname, maternal_surname,
          company, job_title, email, phone_number
      ) ILIKE %s
  )
ORDER BY
    CASE WHEN printed_at IS NULL THEN 0 ELSE 1 END,
    COALESCE(printed_at, created_at) DESC,
    id DESC
LIMIT %s
OFFSET %s
"""

COUNT_PEOPLE_SQL = """
SELECT
    COUNT(*) FILTER (
        WHERE pending_to_print = 0 AND printed_at IS NULL
    )::int AS pending,
    COUNT(*) FILTER (
        WHERE pending_to_print = 1 AND printed_at IS NULL
    )::int AS processing,
    COUNT(*) FILTER (
        WHERE printed_at IS NOT NULL
    )::int AS printed
FROM persons
WHERE is_active = TRUE
  AND (
      %s = ''
      OR concat_ws(
          ' ', first_name, paternal_surname, maternal_surname,
          company, job_title, email, phone_number
      ) ILIKE %s
  )
"""

CLAIM_PERSON_SQL = """
WITH candidate AS (
    SELECT id
    FROM persons
    WHERE id::text = %s
      AND is_active = TRUE
      AND (pending_to_print = 0 OR printed_at IS NOT NULL)
    FOR UPDATE SKIP LOCKED
)
UPDATE persons AS person
SET pending_to_print = 1,
    print_claimed_at = NOW(),
    printed_at = NULL,
    print_attempts = print_attempts + 1,
    print_error = NULL,
    updated_at = NOW()
FROM candidate
WHERE person.id = candidate.id
RETURNING
    person.id,
    person.first_name,
    person.paternal_surname,
    person.maternal_surname,
    person.description,
    person.company,
    person.job_title,
    person.phone_prefix,
    person.phone_number,
    person.email,
    person.consent_at
"""

FORM_FIELDS = (
    "first_name",
    "paternal_surname",
    "maternal_surname",
    "description",
    "company",
    "job_title",
    "phone_prefix",
    "phone_number",
    "email",
    "consent_at",
)

UPPERCASE_FORM_FIELDS = (
    "first_name",
    "paternal_surname",
    "maternal_surname",
    "description",
    "company",
    "job_title",
)


def _configuracion_archivo() -> dict[str, Any]:
    if not RUTA_CONFIG.exists():
        return {}
    with RUTA_CONFIG.open(encoding="utf-8") as archivo:
        contenido = json.load(archivo)
    if not isinstance(contenido, dict):
        raise RuntimeError("config.json debe contener un objeto JSON")
    return contenido


def cargar_configuracion_worker() -> dict[str, Any]:
    if not RUTA_CONFIG_WORKER.exists():
        return {}
    with RUTA_CONFIG_WORKER.open(encoding="utf-8") as archivo:
        contenido = json.load(archivo)
    if not isinstance(contenido, dict):
        raise RuntimeError("worker_config.json debe contener un objeto JSON")
    return contenido


def cargar_printer_id(configuracion: dict[str, Any]) -> str:
    """Resuelve la impresora logica sin requerir cambios en instalaciones legacy."""

    valor = os.getenv("PRINT_PRINTER_ID")
    if valor is None:
        valor = configuracion.get("printer_id")
    return normalizar_printer_id(valor if isinstance(valor, str) else None)


def recurso_fisico_worker(
    transport: str,
    printer_name: str | None,
    bluetooth_port: str | None,
) -> str:
    if transport == "bluetooth":
        assert bluetooth_port is not None
        return f"bluetooth:{bluetooth_port}"
    return nombre_impresora_efectiva(printer_name)


def cargar_url_postgres() -> str:
    url = os.getenv("POSTGRES_DATABASE_URL")
    if not url:
        url = _configuracion_archivo().get("url_db_postgres")
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError(
            "Configura POSTGRES_DATABASE_URL o url_db_postgres en config.json"
        )
    return url.strip()


def cargar_entero(nombre: str, predeterminado: int, minimo: int = 1) -> int:
    contenido = os.getenv(nombre, str(predeterminado))
    try:
        valor = int(contenido)
    except ValueError as error:
        raise RuntimeError(f"{nombre} debe ser un numero entero") from error
    if valor < minimo:
        raise RuntimeError(f"{nombre} debe ser mayor o igual a {minimo}")
    return valor


def cargar_decimal(nombre: str, predeterminado: float) -> float:
    contenido = os.getenv(nombre, str(predeterminado))
    try:
        valor = float(contenido)
    except ValueError as error:
        raise RuntimeError(f"{nombre} debe ser un numero") from error
    if valor <= 0:
        raise RuntimeError(f"{nombre} debe ser mayor que cero")
    return valor


def cargar_booleano(nombre: str, predeterminado: bool = False) -> bool:
    contenido = os.getenv(nombre)
    if contenido is None:
        return predeterminado
    normalizado = contenido.strip().casefold()
    if normalizado in {"1", "true", "yes", "si", "sí"}:
        return True
    if normalizado in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{nombre} debe ser true o false")


class ColaImpresionPostgres:
    def __init__(self, database_url: str, record_order: str = "newest") -> None:
        self.database_url = database_url
        normalized_order = record_order.strip().casefold()
        if normalized_order not in {"newest", "oldest"}:
            raise RuntimeError("record_order debe ser newest u oldest")
        self.record_order = normalized_order

    def _conectar(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "Falta psycopg; instala las dependencias de requirements.txt"
            ) from error
        return psycopg.connect(
            self.database_url,
            connect_timeout=10,
            row_factory=dict_row,
        )

    def reclamar_siguiente(self) -> dict[str, Any] | None:
        direction = "DESC" if self.record_order == "newest" else "ASC"
        query = CLAIM_NEXT_PERSON_SQL.format(order_direction=direction)
        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(query)
                registro = cursor.fetchone()
        return dict(registro) if registro is not None else None

    def buscar_personas(
        self,
        search: str = "",
        status: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        termino = search.strip()
        patron = f"%{termino}%"
        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(
                    SEARCH_PEOPLE_SQL,
                    (
                        status,
                        status,
                        status,
                        status,
                        termino,
                        patron,
                        limit,
                        offset,
                    ),
                )
                registros = cursor.fetchall()
        return [dict(registro) for registro in registros]

    def contar_personas(self, search: str = "") -> dict[str, int]:
        termino = search.strip()
        patron = f"%{termino}%"
        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(COUNT_PEOPLE_SQL, (termino, patron))
                conteos = cursor.fetchone()
        return {
            "pending": int(conteos["pending"]),
            "processing": int(conteos["processing"]),
            "printed": int(conteos["printed"]),
        }

    def reclamar_persona(self, person_id: Any) -> dict[str, Any] | None:
        """Reserva una persona concreta para impresion o reimpresion manual."""

        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(CLAIM_PERSON_SQL, (person_id,))
                registro = cursor.fetchone()
        return dict(registro) if registro is not None else None

    def marcar_impresa(self, person_id: Any) -> None:
        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(MARK_PRINTED_SQL, (person_id,))
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"No se pudo marcar persons.id={person_id!s} como printed"
                    )

    def marcar_fallida(self, person_id: Any, error: str) -> None:
        detalle = error.strip()[:2000] or "Error de impresion sin detalle"
        with self._conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(MARK_FAILED_SQL, (detalle, person_id))
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"No se pudo marcar persons.id={person_id!s} como failed"
                    )


def formulario_desde_persona(
    persona: dict[str, Any],
    printer_name: str | None = None,
) -> Formulario:
    datos = {campo: persona.get(campo) for campo in FORM_FIELDS}
    for campo in UPPERCASE_FORM_FIELDS:
        if isinstance(datos[campo], str):
            datos[campo] = datos[campo].strip().upper()
    if isinstance(datos["email"], str):
        datos["email"] = datos["email"].strip().lower()
    datos["printer_name"] = printer_name
    # Los registros de PostgreSQL son la fuente de impresion. Se construye el
    # modelo sin ejecutar validadores para no rechazar correos reservados, telefonos
    # heredados, campos vacios u otros datos que ya fueron almacenados.
    return Formulario.model_construct(**datos)


def descripcion_error(error: Exception) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)
    if isinstance(error, ValidationError):
        return str(error)
    return str(error) or error.__class__.__name__


def procesar_persona(
    cola: ColaImpresionPostgres,
    persona: dict[str, Any],
    simulate: bool,
    printer_name: str | None,
    transport: str = "windows",
    bluetooth_port: str | None = None,
    bluetooth_baudrate: int = 9600,
    printer_id: str = "default",
) -> bool:
    person_id = persona["id"]
    try:
        formulario = formulario_desde_persona(persona, printer_name)
        resultado = procesar_impresion(
            formulario,
            simulate=simulate,
            transport=transport,
            bluetooth_port=bluetooth_port,
            bluetooth_baudrate=bluetooth_baudrate,
            printer_id=printer_id,
        )
    except Exception as error:
        detalle = descripcion_error(error)
        LOGGER.exception("Fallo la impresion de la persona %s: %s", person_id, detalle)
        try:
            cola.marcar_fallida(person_id, detalle)
        except Exception:
            LOGGER.exception(
                "No se pudo registrar el fallo de la persona %s en PostgreSQL",
                person_id,
            )
        return False

    try:
        cola.marcar_impresa(person_id)
    except Exception:
        # La impresion ya pudo haber salido fisicamente. Se conserva processing para
        # una conciliacion manual y asi no convertir un fallo de red en un duplicado.
        LOGGER.exception(
            "La persona %s se imprimio, pero no pudo marcarse como printed; "
            "queda en processing para revision manual",
            person_id,
        )
        return False

    LOGGER.info(
        "Persona %s impresa en tira %s, posicion %s%s",
        person_id,
        resultado["strip_id"],
        resultado["position"],
        " (simulacion)" if simulate else "",
    )
    return True


def ejecutar_ciclo(
    cola: ColaImpresionPostgres,
    batch_size: int,
    simulate: bool,
    printer_name: str | None,
    transport: str = "windows",
    bluetooth_port: str | None = None,
    bluetooth_baudrate: int = 9600,
    printer_id: str = "default",
) -> int:
    procesadas = 0
    # La impresora es un recurso fisico exclusivo: cada ciclo reclama como maximo
    # una persona, incluso si una configuracion antigua solicita un lote mayor.
    for _ in range(min(batch_size, 1)):
        persona = cola.reclamar_siguiente()
        if persona is None:
            break
        procesar_persona(
            cola,
            persona,
            simulate,
            printer_name,
            transport,
            bluetooth_port,
            bluetooth_baudrate,
            printer_id,
        )
        procesadas += 1
    return procesadas


def ejecutar_worker(stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    config = cargar_configuracion_worker()
    printer_id = cargar_printer_id(config)
    record_order = str(config.get("record_order", "newest"))
    cola = ColaImpresionPostgres(cargar_url_postgres(), record_order)
    poll_seconds = cargar_decimal(
        "PRINT_POLL_SECONDS",
        float(config.get("poll_seconds", 3.0)),
    )
    batch_size = 1
    simulate = cargar_booleano(
        "PRINT_SIMULATE",
        bool(config.get("simulate", False)),
    )
    printer_name = (
        os.getenv("PRINT_PRINTER_NAME")
        or config.get("printer_name")
        or None
    )
    transport = os.getenv(
        "PRINT_TRANSPORT",
        str(config.get("transport", "windows")),
    ).strip().casefold()
    if transport not in {"windows", "bluetooth"}:
        raise RuntimeError("PRINT_TRANSPORT debe ser windows o bluetooth")
    bluetooth_port = (
        os.getenv("BLUETOOTH_COM_PORT")
        or config.get("bluetooth_com_port")
        or None
    )
    bluetooth_baudrate = cargar_entero(
        "BLUETOOTH_BAUDRATE",
        int(config.get("bluetooth_baudrate", 9600)),
        1200,
    )
    if transport == "bluetooth" and not bluetooth_port:
        raise RuntimeError(
            "Configura BLUETOOTH_COM_PORT cuando PRINT_TRANSPORT=bluetooth"
        )

    print_all = bool(config.get("print_all", False))
    max_records = int(config.get("max_records", 1))
    if max_records < 1:
        raise RuntimeError("max_records debe ser mayor o igual a 1")

    recurso_fisico = recurso_fisico_worker(
        transport,
        printer_name,
        bluetooth_port,
    )
    # El orden logico -> fisico es fijo para evitar deadlocks entre workers.
    with poseer_printer_id(printer_id), poseer_impresora(recurso_fisico):
        LOGGER.info(
            "Worker iniciado: transporte=%s, orden=%s, todos=%s, maximo=%s, "
            "intervalo=%ss, lote=%s, simulacion=%s, printer_id=%s, printer_name=%s",
            transport,
            record_order,
            print_all,
            max_records,
            poll_seconds,
            batch_size,
            simulate,
            printer_id,
            printer_name,
        )
        heartbeat_stop = threading.Event()

        def mantener_heartbeat() -> None:
            while not heartbeat_stop.is_set():
                try:
                    registrar_heartbeat_worker(printer_id=printer_id)
                except Exception:
                    LOGGER.exception("No se pudo actualizar la senal de vida del worker")
                heartbeat_stop.wait(2.0)

        heartbeat_thread = threading.Thread(
            target=mantener_heartbeat,
            name="worker-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        total_processed = 0
        estaba_pausado = False
        try:
            while not stop_event.is_set():
                inicio = monotonic()
                try:
                    control = obtener_control_worker()
                except Exception:
                    LOGGER.exception("No se pudo consultar el control local del worker")
                    stop_event.wait(min(poll_seconds, 1.0))
                    continue
                if not control.enabled:
                    if not estaba_pausado:
                        LOGGER.info("Worker pausado desde la consola de impresion manual")
                    estaba_pausado = True
                    stop_event.wait(min(poll_seconds, 1.0))
                    continue
                if estaba_pausado:
                    LOGGER.info("Worker reanudado desde la consola de impresion manual")
                    estaba_pausado = False
                try:
                    procesadas = ejecutar_ciclo(
                        cola,
                        batch_size=batch_size,
                        simulate=simulate,
                        printer_name=printer_name,
                        transport=transport,
                        bluetooth_port=bluetooth_port,
                        bluetooth_baudrate=bluetooth_baudrate,
                        printer_id=printer_id,
                    )
                    total_processed += procesadas
                except Exception:
                    procesadas = 0
                    LOGGER.exception(
                        "No se pudo consultar la cola de impresion en PostgreSQL"
                    )

                if not print_all and total_processed >= max_records:
                    LOGGER.info(
                        "Limite controlado alcanzado: %s registro(s) procesado(s)",
                        total_processed,
                    )
                    break

                transcurrido = monotonic() - inicio
                stop_event.wait(max(0.0, poll_seconds - transcurrido))
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=3.0)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop_event = threading.Event()

    def detener(_signum, _frame) -> None:
        LOGGER.info("Deteniendo worker de impresion")
        stop_event.set()

    signal.signal(signal.SIGINT, detener)
    signal.signal(signal.SIGTERM, detener)
    ejecutar_worker(stop_event)


if __name__ == "__main__":
    main()
