import json
import os
import sqlite3
from contextlib import closing
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import uuid4

import qrcode
import win32con
import win32print
import win32ui
from fastapi import FastAPI, HTTPException, Path as PathParameter, Query, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from PIL import Image, ImageDraw, ImageFont, ImageWin
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from thermal_printer import (
    DEFAULT_BAUDRATE,
    generar_imagen_termica,
    imprimir_bluetooth as enviar_bluetooth,
    listar_puertos_seriales,
)


TAGS_METADATA = [
    {
        "name": "Estado",
        "description": "Comprobacion de disponibilidad y datos basicos del servicio.",
    },
    {
        "name": "Formularios",
        "description": (
            "Registro y consulta de los datos capturados para cada asistente, "
            "incluida la generacion de su codigo QR."
        ),
    },
    {
        "name": "Impresion",
        "description": (
            "Previsualizacion de gafetes y comunicacion con las impresoras "
            "instaladas en Windows."
        ),
    },
    {
        "name": "Vistas web",
        "description": "Paginas HTML para previsualizar y consultar formularios desde un navegador.",
    },
]


app = FastAPI(
    title="API de impresion de gafetes Nexus",
    version="1.1.0",
    description=(
        "API local para registrar asistentes y asignar automaticamente sus gafetes a "
        "exclusivamente la posicion 2 de una tira vertical de media hoja Carta.\n\n"
        "### Flujo recomendado\n"
        "1. Envia el formulario a `POST /print`.\n"
        "2. La API reserva en SQLite las posiciones `1`, `2`, `3` y luego abre otra tira.\n"
        "3. Usa `GET /print-state` para consultar la secuencia.\n"
        "4. Usa `PUT /print-state` para corregir una posicion o iniciar otra tira.\n\n"
        "`POST /print` funciona en simulacion por defecto. Para usar el controlador "
        "grafico de Windows envia `simulate=false`. Los campos desconocidos son "
        "rechazados y las fechas deben usar formato ISO 8601."
    ),
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
    contact={"name": "Nexus"},
    license_info={"name": "Uso interno"},
)

DIRECTORIO_BASE = Path(__file__).resolve().parent
RUTA_CONFIG = DIRECTORIO_BASE / "config.json"
DIRECTORIO_DATOS = DIRECTORIO_BASE / "data"
RUTA_DB = DIRECTORIO_DATOS / "forms.db"


def cargar_url_publica() -> str:
    configuracion: dict[str, str] = {}
    if RUTA_CONFIG.exists():
        with RUTA_CONFIG.open(encoding="utf-8") as archivo:
            configuracion = json.load(archivo)
    url = os.getenv(
        "PUBLIC_BASE_URL",
        configuracion.get("public_base", "http://127.0.0.1:9102"),
    )
    return url.rstrip("/")


PUBLIC_BASE_URL = cargar_url_publica()
NETWORKING_URL = "https://www.expoteleinfo.com/networking"

TextoRequerido = Annotated[str, Field(min_length=1, max_length=100)]


class Formulario(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "first_name": "Ana",
                "paternal_surname": "Perez",
                "maternal_surname": "Lopez",
                "description": "Invitada VIP",
                "company": "Nexus",
                "job_title": "Gerente comercial",
                "phone_prefix": "+591",
                "phone_number": "71234567",
                "email": "ana.perez@ejemplo.com",
                "consent_at": "2026-08-31T10:30:00-04:00",
            }
        },
    )

    first_name: Annotated[
        TextoRequerido,
        Field(description="Nombre del asistente.", examples=["Ana"]),
    ]
    paternal_surname: Annotated[
        TextoRequerido,
        Field(description="Apellido paterno del asistente.", examples=["Perez"]),
    ]
    maternal_surname: Annotated[
        str | None,
        Field(max_length=100, description="Apellido materno.", examples=["Lopez"]),
    ] = None
    description: Annotated[
        str | None,
        Field(
            max_length=500,
            description="Nota o descripcion adicional del asistente.",
            examples=["Invitada VIP"],
        ),
    ] = None
    company: Annotated[
        str | None,
        Field(max_length=100, description="Empresa u organizacion.", examples=["Nexus"]),
    ] = None
    job_title: Annotated[
        str | None,
        Field(max_length=100, description="Cargo profesional.", examples=["Gerente comercial"]),
    ] = None
    phone_prefix: Annotated[
        str | None,
        Field(
            pattern=r"^\+[1-9]\d{0,3}$",
            description="Prefijo telefonico internacional con signo +.",
            examples=["+591"],
        ),
    ] = None
    phone_number: Annotated[
        str | None,
        Field(
            pattern=r"^\d{6,15}$",
            description="Numero telefonico de 6 a 15 digitos, sin espacios.",
            examples=["71234567"],
        ),
    ] = None
    email: Annotated[
        EmailStr | None,
        Field(description="Correo electronico del asistente.", examples=["ana.perez@ejemplo.com"]),
    ] = None
    consent_at: Annotated[
        datetime | None,
        Field(
            description="Fecha y hora ISO 8601 en que se otorgo el consentimiento.",
            examples=["2026-08-31T10:30:00-04:00"],
        ),
    ] = None
    printer_name: Annotated[
        str | None,
        Field(
            max_length=255,
            description=(
                "Nombre exacto de la impresora de Windows. Solo se usa al imprimir; "
                "si se omite, se utiliza la impresora predeterminada."
            ),
            examples=["EPSON L3310 Series"],
        ),
    ] = None

    @field_validator("first_name", "paternal_surname")
    @classmethod
    def limpiar_requerido(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("El campo no puede estar vacio")
        return value.upper()

    @field_validator(
        "maternal_surname",
        "description",
        "company",
        "job_title",
        "phone_prefix",
        "phone_number",
        "printer_name",
        mode="before",
    )
    @classmethod
    def limpiar_opcional(cls, value: object) -> object:
        if not isinstance(value, str):
            return None
        return value.strip() or None

    @field_validator("email", mode="before")
    @classmethod
    def limpiar_correo(cls, value: object) -> object:
        if not isinstance(value, str):
            return None
        return value.strip().lower() or None

    @field_validator("maternal_surname", "description", "company", "job_title")
    @classmethod
    def convertir_texto_a_mayusculas(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None


class EstadoServicio(BaseModel):
    status: str = Field(description="Estado actual del servicio.", examples=["ok"])
    service: str = Field(description="Nombre del servicio.")


class DatosFormulario(BaseModel):
    """Datos persistidos de un formulario (la impresora nunca se almacena)."""

    first_name: str
    paternal_surname: str
    maternal_surname: str | None = None
    description: str | None = None
    company: str | None = None
    job_title: str | None = None
    phone_prefix: str | None = None
    phone_number: str | None = None
    email: EmailStr | None = None
    consent_at: datetime | None = None


class FormularioCreado(BaseModel):
    ok: bool = Field(description="Indica que el registro se creo correctamente.")
    id: str = Field(description="Identificador UUID del formulario.")
    created_at: datetime = Field(description="Fecha y hora de creacion en formato ISO 8601.")
    view_url: str = Field(description="URL publica de la vista HTML del formulario.")
    qr_url: str = Field(description="URL publica de la imagen PNG del codigo QR.")
    print_preview_url: str = Field(
        description="URL de la imagen PNG con las medidas exactas de impresion."
    )
    data: DatosFormulario = Field(description="Datos almacenados; no incluye printer_name.")


class FormularioConsultado(DatosFormulario):
    id: str = Field(description="Identificador UUID del formulario.")
    created_at: datetime = Field(description="Fecha y hora de creacion en formato ISO 8601.")


class ListaImpresoras(BaseModel):
    printers: list[str] = Field(description="Impresoras locales y conectadas disponibles.")
    default: str | None = Field(description="Impresora predeterminada de Windows.")


class PuertoSerial(BaseModel):
    device: str = Field(description="Puerto serie asignado por Windows, por ejemplo COM5.")
    description: str | None = Field(description="Descripcion publicada por el dispositivo.")
    hwid: str | None = Field(description="Identificador de hardware informado por Windows.")


class ListaPuertosSeriales(BaseModel):
    ports: list[PuertoSerial] = Field(
        description="Puertos COM disponibles, incluidos los Bluetooth emparejados."
    )


class ImpresionEnviada(BaseModel):
    ok: bool = Field(description="Indica que el trabajo fue aceptado por Windows.")
    message: str = Field(description="Resultado legible de la operacion.")
    printer: str = Field(description="Nombre de la impresora que recibio el trabajo.")
    id: str = Field(description="Identificador UUID del formulario guardado.")
    view_url: str = Field(description="URL publica de la vista HTML del formulario.")
    qr_url: str = Field(description="URL publica de la imagen del codigo QR.")
    print_preview_url: str = Field(description="URL de la imagen exacta enviada a imprimir.")


class ConfiguracionTira(BaseModel):
    """Medidas y calibracion persistente de la media hoja Carta."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "paper_width_mm": 107.95,
                "paper_height_mm": 300,
                "badge_width_mm": 100,
                "badge_height_mm": 80,
                "form_padding_left_mm": 6,
                "global_offset_x_mm": 0,
                "global_offset_y_mm": -1,
            }
        },
    )

    paper_width_mm: float = Field(default=107.95, gt=50, le=220, description="Ancho fisico de la tira en milimetros.")
    paper_height_mm: float = Field(default=279.4, gt=100, le=500, description="Alto fisico de la tira en milimetros.")
    badge_width_mm: float = Field(default=100, gt=20, le=220, description="Ancho de cada cuadro luego de girar el formulario.")
    badge_height_mm: float = Field(default=80, gt=20, le=160, description="Alto ocupado por cada una de las tres posiciones.")
    form_padding_left_mm: float = Field(
        default=6,
        ge=0,
        le=15,
        description="Margen interno que desplaza el contenido del gafete hacia la derecha.",
    )
    global_offset_x_mm: float = Field(default=0, ge=-30, le=30, description="Correccion horizontal aplicada a todos los trabajos.")
    global_offset_y_mm: float = Field(default=-1, ge=-30, le=30, description="Correccion vertical aplicada a todos los trabajos.")

    @model_validator(mode="after")
    def validar_distribucion(self) -> "ConfiguracionTira":
        if self.badge_width_mm > self.paper_width_mm:
            raise ValueError("El ancho del gafete no puede superar el ancho de la tira")
        if self.badge_height_mm * 3 > self.paper_height_mm:
            raise ValueError("Los tres gafetes no caben en el alto de la tira")
        return self


class ImpresionPosicion(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "position": 2,
                "offset_x_mm": 1.5,
                "offset_y_mm": -2,
                "printer_name": "EPSON L3310 Series",
            }
        },
    )

    position: int = Field(
        default=2,
        ge=1,
        le=3,
        description="Posicion solicitada; se sobrescribe siempre con la posicion 2.",
    )
    offset_x_mm: float = Field(default=0, ge=-20, le=20)
    offset_y_mm: float = Field(default=0, ge=-20, le=20)
    printer_name: str | None = Field(default=None, max_length=255)

    @field_validator("printer_name", mode="before")
    @classmethod
    def limpiar_impresora(cls, value: object) -> object:
        if not isinstance(value, str):
            return None
        return value.strip() or None


class ImpresionPosicionEnviada(BaseModel):
    ok: bool
    message: str
    printer: str
    id: str
    position: int
    offset_x_mm: float
    offset_y_mm: float


class RegistroPosicion(BaseModel):
    position: int
    form_id: str
    status: str
    data: DatosFormulario


class EstadoPosiciones(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "strip_id": "c7bfbc4c-f970-49b1-a2f8-e1dd2450cdda",
                "next_position": 2,
                "positions": [
                    {
                        "position": 2,
                        "form_id": "a18a826c-e6cd-4b83-b70e-470811a6a2f3",
                        "status": "simulated",
                        "data": {
                            "first_name": "Ana",
                            "paternal_surname": "Perez",
                            "company": "Nexus",
                        },
                    }
                ],
            }
        }
    )

    strip_id: str = Field(description="UUID de la tira actual o de la ultima completada.")
    next_position: int = Field(description="Posicion que reservara la siguiente solicitud a POST /print.")
    positions: list[RegistroPosicion] = Field(description="Ultimo trabajo registrado en cada posicion de la tira.")


class AjusteEstadoPosiciones(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"next_position": 2, "start_new_strip": False},
                {"next_position": 2, "start_new_strip": True},
            ]
        },
    )

    next_position: int = Field(
        default=2,
        ge=1,
        le=3,
        description="Valor solicitado; la API conserva siempre la posicion 2.",
    )
    start_new_strip: bool = Field(default=False, description="Si es true, abandona la tira activa y crea otra.")


class ImpresionAutomaticaEnviada(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ok": True,
                "message": "Simulacion asignada a la posicion 2",
                "simulated": True,
                "printer": None,
                "id": "a18a826c-e6cd-4b83-b70e-470811a6a2f3",
                "strip_id": "c7bfbc4c-f970-49b1-a2f8-e1dd2450cdda",
                "position": 2,
                "next_position": 2,
                "strip_completed": False,
                "view_url": "http://192.168.21.83:9101/forms/a18a826c-e6cd-4b83-b70e-470811a6a2f3",
            }
        }
    )

    ok: bool = Field(description="Indica que el formulario y la posicion fueron registrados.")
    message: str = Field(description="Resultado legible de la operacion.")
    simulated: bool = Field(description="Indica que no se contacto al controlador de Windows.")
    printer: str | None = Field(description="Impresora utilizada; null durante una simulacion.")
    id: str = Field(description="UUID del formulario guardado.")
    strip_id: str = Field(description="UUID de la tira asignada.")
    position: int = Field(description="Posicion reservada: 1, 2 o 3.")
    next_position: int = Field(description="Posicion prevista para la siguiente solicitud.")
    strip_completed: bool = Field(description="Indica que esta solicitud completo la posicion 3.")
    view_url: str = Field(description="Vista publica del formulario guardado.")


class PersonaParaImpresion(BaseModel):
    id: str
    first_name: str | None = None
    paternal_surname: str | None = None
    maternal_surname: str | None = None
    description: str | None = None
    company: str | None = None
    job_title: str | None = None
    phone_prefix: str | None = None
    phone_number: str | None = None
    email: str | None = None
    consent_at: datetime | None = None
    print_state: Literal["pending", "printed"]
    printed_at: datetime | None = None
    print_error: str | None = None


class ListaPersonasParaImpresion(BaseModel):
    people: list[PersonaParaImpresion]
    count: int


class ImpresionManualPersona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    simulate: bool = True
    printer_name: str | None = Field(default=None, max_length=255)

    @field_validator("printer_name", mode="before")
    @classmethod
    def limpiar_impresora(cls, value: object) -> object:
        if not isinstance(value, str):
            return None
        return value.strip() or None


class RespuestaError(BaseModel):
    detail: str = Field(description="Descripcion del error.")


RESPUESTA_VALIDACION = {
    "description": "El cuerpo o los parametros no cumplen el contrato.",
}


def conectar_db() -> sqlite3.Connection:
    DIRECTORIO_DATOS.mkdir(exist_ok=True)
    conexion = sqlite3.connect(RUTA_DB, timeout=10)
    conexion.row_factory = sqlite3.Row
    return conexion


def inicializar_db() -> None:
    with closing(conectar_db()) as conexion:
        with conexion:
            conexion.execute(
                """
                CREATE TABLE IF NOT EXISTS forms (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conexion.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conexion.execute(
                """
                CREATE TABLE IF NOT EXISTS print_strips (
                    id TEXT PRIMARY KEY,
                    next_position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conexion.execute(
                """
                CREATE TABLE IF NOT EXISTS print_jobs (
                    id TEXT PRIMARY KEY,
                    strip_id TEXT NOT NULL,
                    form_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY (strip_id) REFERENCES print_strips(id),
                    FOREIGN KEY (form_id) REFERENCES forms(id)
                )
                """
            )


CLAVE_CONFIGURACION_TIRA = "print_layout"


def obtener_configuracion_tira() -> ConfiguracionTira:
    with closing(conectar_db()) as conexion:
        fila = conexion.execute(
            "SELECT value FROM settings WHERE key = ?",
            (CLAVE_CONFIGURACION_TIRA,),
        ).fetchone()
    if fila is None:
        return ConfiguracionTira()
    return ConfiguracionTira.model_validate_json(fila["value"])


def guardar_configuracion_tira(
    configuracion: ConfiguracionTira,
) -> ConfiguracionTira:
    contenido = configuracion.model_dump_json()
    with closing(conectar_db()) as conexion:
        with conexion:
            conexion.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (CLAVE_CONFIGURACION_TIRA, contenido),
            )
    return configuracion


def guardar_formulario(data: Formulario) -> tuple[str, str]:
    form_id = str(uuid4())
    creado = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = json.dumps(
        data.model_dump(mode="json", exclude={"printer_name"}),
        ensure_ascii=False,
    )
    with closing(conectar_db()) as conexion:
        with conexion:
            conexion.execute(
                "INSERT INTO forms (id, payload, created_at) VALUES (?, ?, ?)",
                (form_id, payload, creado),
            )
    return form_id, creado


def obtener_formulario(form_id: str) -> dict[str, object] | None:
    with closing(conectar_db()) as conexion:
        fila = conexion.execute(
            "SELECT payload, created_at FROM forms WHERE id = ?",
            (form_id,),
        ).fetchone()
    if fila is None:
        return None
    return {
        "id": form_id,
        "created_at": fila["created_at"],
        **json.loads(fila["payload"]),
    }


def url_formulario(form_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/forms/{form_id}"


def url_networking(data: Formulario) -> str:
    """Construye el destino del QR con los datos capturados del asistente."""

    apellido = " ".join(
        parte
        for parte in (data.paternal_surname, data.maternal_surname)
        if parte
    )
    telefono = "".join(
        parte for parte in (data.phone_prefix, data.phone_number) if parte
    )
    parametros = {
        "nombre": data.first_name,
        "apellido": apellido,
        "telefono": telefono,
        "email": str(data.email) if data.email else "",
        "cargo": data.job_title or "",
        "empresa": data.company or "",
    }
    return f"{NETWORKING_URL}?{urlencode(parametros)}"


def formulario_desde_registro(registro: dict[str, object]) -> Formulario:
    datos = {
        campo: registro.get(campo)
        for campo in Formulario.model_fields
        if campo != "printer_name"
    }
    return Formulario.model_validate(datos)


POSICION_IMPRESION = 3


def _crear_tira(
    conexion: sqlite3.Connection,
    next_position: int = POSICION_IMPRESION,
) -> str:
    strip_id = str(uuid4())
    creado = datetime.now().astimezone().isoformat(timespec="seconds")
    conexion.execute(
        """
        INSERT INTO print_strips (id, next_position, status, created_at)
        VALUES (?, ?, 'active', ?)
        """,
        (strip_id, next_position, creado),
    )
    return strip_id


def _estado_tira(
    conexion: sqlite3.Connection,
    strip_id: str,
) -> dict[str, object]:
    tira = conexion.execute(
        "SELECT next_position FROM print_strips WHERE id = ?",
        (strip_id,),
    ).fetchone()
    trabajos = conexion.execute(
        """
        SELECT trabajo.position, trabajo.form_id, trabajo.status, formulario.payload
        FROM print_jobs AS trabajo
        JOIN forms AS formulario ON formulario.id = trabajo.form_id
        WHERE trabajo.strip_id = ?
          AND trabajo.rowid = (
              SELECT MAX(ultimo.rowid)
              FROM print_jobs AS ultimo
              WHERE ultimo.strip_id = trabajo.strip_id
                AND ultimo.position = trabajo.position
          )
        ORDER BY trabajo.position
        """,
        (strip_id,),
    ).fetchall()
    return {
        "strip_id": strip_id,
        "next_position": int(tira["next_position"]),
        "positions": [
            {
                "position": int(trabajo["position"]),
                "form_id": trabajo["form_id"],
                "status": trabajo["status"],
                "data": json.loads(trabajo["payload"]),
            }
            for trabajo in trabajos
        ],
    }


def obtener_estado_posiciones() -> dict[str, object]:
    with closing(conectar_db()) as conexion:
        conexion.execute("BEGIN IMMEDIATE")
        try:
            tira = conexion.execute(
                """
                SELECT id FROM print_strips
                WHERE status = 'active'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if tira:
                strip_id = tira["id"]
            else:
                ultima = conexion.execute(
                    """
                    SELECT id FROM print_strips
                    WHERE status = 'completed'
                    ORDER BY completed_at DESC LIMIT 1
                    """
                ).fetchone()
                strip_id = ultima["id"] if ultima else _crear_tira(conexion)
            estado = _estado_tira(conexion, strip_id)
            conexion.commit()
            return estado
        except Exception:
            conexion.rollback()
            raise


def ajustar_estado_posiciones(
    ajuste: AjusteEstadoPosiciones,
) -> dict[str, object]:
    with closing(conectar_db()) as conexion:
        conexion.execute("BEGIN IMMEDIATE")
        try:
            tira = conexion.execute(
                "SELECT id FROM print_strips WHERE status = 'active' LIMIT 1"
            ).fetchone()
            if ajuste.start_new_strip and tira:
                conexion.execute(
                    "UPDATE print_strips SET status = 'abandoned' WHERE id = ?",
                    (tira["id"],),
                )
                tira = None

            if tira is None:
                strip_id = _crear_tira(conexion, POSICION_IMPRESION)
            else:
                strip_id = tira["id"]
                conexion.execute(
                    "UPDATE print_strips SET next_position = ? WHERE id = ?",
                    (POSICION_IMPRESION, strip_id),
                )
            estado = _estado_tira(conexion, strip_id)
            conexion.commit()
            return estado
        except Exception:
            conexion.rollback()
            raise


def reservar_posicion(form_id: str) -> tuple[str, str, int, int, bool]:
    with closing(conectar_db()) as conexion:
        conexion.execute("BEGIN IMMEDIATE")
        try:
            tira = conexion.execute(
                "SELECT id, next_position FROM print_strips WHERE status = 'active' LIMIT 1"
            ).fetchone()
            if tira is None:
                strip_id = _crear_tira(conexion)
            else:
                strip_id = tira["id"]
            position = POSICION_IMPRESION

            job_id = str(uuid4())
            creado = datetime.now().astimezone().isoformat(timespec="seconds")
            conexion.execute(
                """
                INSERT INTO print_jobs
                    (id, strip_id, form_id, position, status, created_at)
                VALUES (?, ?, ?, ?, 'reserved', ?)
                """,
                (job_id, strip_id, form_id, position, creado),
            )

            completa = True
            siguiente = POSICION_IMPRESION
            conexion.execute(
                """
                UPDATE print_strips
                SET next_position = ?, status = 'completed', completed_at = ?
                WHERE id = ?
                """,
                (POSICION_IMPRESION, creado, strip_id),
            )
            conexion.commit()
            return job_id, strip_id, position, siguiente, completa
        except Exception:
            conexion.rollback()
            raise


def actualizar_trabajo_impresion(
    job_id: str,
    estado: str,
    error: str | None = None,
) -> None:
    with closing(conectar_db()) as conexion:
        with conexion:
            conexion.execute(
                "UPDATE print_jobs SET status = ?, error = ? WHERE id = ?",
                (estado, error, job_id),
            )


def fallar_trabajo_y_devolver_posicion(job_id: str, error: str) -> bool:
    """Marca el trabajo fallido y recupera su posicion cuando sigue siendo la ultima."""

    with closing(conectar_db()) as conexion:
        conexion.execute("BEGIN IMMEDIATE")
        try:
            trabajo = conexion.execute(
                """
                SELECT rowid, strip_id, position
                FROM print_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if trabajo is None:
                conexion.commit()
                return False

            conexion.execute(
                "UPDATE print_jobs SET status = 'failed', error = ? WHERE id = ?",
                (error, job_id),
            )

            trabajo_posterior = conexion.execute(
                """
                SELECT 1 FROM print_jobs
                WHERE strip_id = ? AND rowid > ?
                LIMIT 1
                """,
                (trabajo["strip_id"], trabajo["rowid"]),
            ).fetchone()
            if trabajo_posterior:
                conexion.commit()
                return False

            posicion = int(trabajo["position"])
            if posicion in (2, 3):
                otra_tira_activa = conexion.execute(
                    """
                    SELECT 1 FROM print_strips
                    WHERE status = 'active' AND id <> ?
                    LIMIT 1
                    """,
                    (trabajo["strip_id"],),
                ).fetchone()
                if otra_tira_activa:
                    conexion.commit()
                    return False
                cursor = conexion.execute(
                    """
                    UPDATE print_strips
                    SET next_position = ?, status = 'active', completed_at = NULL
                    WHERE id = ? AND status = 'completed'
                    """,
                    (posicion, trabajo["strip_id"]),
                )
            else:
                cursor = conexion.execute(
                    """
                    UPDATE print_strips
                    SET next_position = ?
                    WHERE id = ? AND status = 'active' AND next_position = ?
                    """,
                    (posicion, trabajo["strip_id"], posicion + 1),
                )

            recuperada = cursor.rowcount == 1
            conexion.commit()
            return recuperada
        except Exception:
            conexion.rollback()
            raise


inicializar_db()


# Medidas del soporte individual y del sticker, en milimetros.
ANCHO = 40
PAPEL_ANCHO_MM = 109
PAPEL_ALTO_MM = 100
STICKER_ANCHO_MM = 85
DPI_RENDER = 300
QR_TAMANO_MM = 17
QR_MARGEN_INFERIOR_MM = 6
QR_MARGEN_DERECHO_MM = 2
QR_DESPLAZAMIENTO_ARRIBA_MM = 3
PADDING_ADICIONAL_POR_POSICION_MM = {1: 4, 2: 2, 3: 0}
FUENTE_REGULAR = Path("C:/Windows/Fonts/arial.ttf")
FUENTE_NEGRITA = Path("C:/Windows/Fonts/arialbd.ttf")


def centrar(texto: str) -> str:
    return texto.center(ANCHO).rstrip()


def generar_formulario(data: Formulario) -> str:
    apellidos = " ".join(
        parte
        for parte in (data.paternal_surname, data.maternal_surname)
        if parte
    )
    partes = [centrar(data.first_name), centrar(apellidos)]
    if data.company:
        partes.append(centrar(data.company))
    if data.job_title:
        partes.append(centrar(data.job_title))
    if data.email:
        partes.extend(("", centrar(str(data.email))))
    partes.append("\n\n")
    return "\n".join(partes)


def mm_a_px(milimetros: float) -> int:
    return round(milimetros * DPI_RENDER / 25.4)


def padding_formulario_mm(position: int, padding_base_mm: float) -> float:
    """Aumenta gradualmente el margen izquierdo desde la posicion 3 a la 1."""

    return padding_base_mm + PADDING_ADICIONAL_POR_POSICION_MM[position]


def puntos_a_px(puntos: float) -> int:
    return round(puntos * DPI_RENDER / 72)


def cargar_fuente(negrita: bool, puntos: float) -> ImageFont.FreeTypeFont:
    ruta = FUENTE_NEGRITA if negrita else FUENTE_REGULAR
    return ImageFont.truetype(str(ruta), puntos_a_px(puntos))


def fuente_ajustada(
    dibujo: ImageDraw.ImageDraw,
    texto: str,
    puntos: float,
    minimo: float,
    ancho_maximo: int,
    negrita: bool,
) -> ImageFont.FreeTypeFont:
    tamano = puntos
    while tamano > minimo:
        fuente = cargar_fuente(negrita, tamano)
        caja = dibujo.textbbox((0, 0), texto, font=fuente)
        if caja[2] - caja[0] <= ancho_maximo:
            return fuente
        tamano -= 0.5
    return cargar_fuente(negrita, minimo)


def dibujar_centrado(
    dibujo: ImageDraw.ImageDraw,
    texto: str,
    y_mm: float,
    fuente: ImageFont.FreeTypeFont,
    color: str,
) -> None:
    caja = dibujo.textbbox((0, 0), texto, font=fuente)
    ancho = caja[2] - caja[0]
    x = (mm_a_px(PAPEL_ANCHO_MM) - ancho) / 2
    dibujo.text((x, mm_a_px(y_mm)), texto, font=fuente, fill=color)


def dibujar_campo(
    dibujo: ImageDraw.ImageDraw,
    etiqueta: str,
    valor: str | None,
    y_etiqueta_mm: float,
    y_valor_mm: float,
    puntos: float,
    minimo: float,
    negrita: bool,
) -> None:
    if not valor:
        return
    ancho_util = mm_a_px(STICKER_ANCHO_MM - 6)
    fuente_etiqueta = cargar_fuente(False, 4.5)
    fuente_valor = fuente_ajustada(
        dibujo, valor, puntos, minimo, ancho_util, negrita
    )
    dibujar_centrado(
        dibujo, etiqueta.upper(), y_etiqueta_mm, fuente_etiqueta, "#6b7774"
    )
    dibujar_centrado(dibujo, valor, y_valor_mm, fuente_valor, "#17201f")


def generar_codigo_qr(url: str, tamano_mm: float = QR_TAMANO_MM) -> Image.Image:
    """Genera un QR compacto y de alto contraste apto para impresion."""

    codigo = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    codigo.add_data(url)
    codigo.make(fit=True)
    imagen = codigo.make_image(fill_color="black", back_color="white").convert("RGB")
    tamano_px = mm_a_px(tamano_mm)
    return imagen.resize((tamano_px, tamano_px), Image.Resampling.NEAREST)


def generar_imagen_impresion(
    data: Formulario,
    form_id: str,
    incluir_qr: bool = True,
) -> Image.Image:
    imagen = Image.new(
        "RGB",
        (mm_a_px(PAPEL_ANCHO_MM), mm_a_px(PAPEL_ALTO_MM)),
        "white",
    )
    dibujo = ImageDraw.Draw(imagen)
    apellidos = " ".join(
        parte
        for parte in (data.paternal_surname, data.maternal_surname)
        if parte
    )

    dibujar_campo(dibujo, "Nombre", data.first_name, 3, 6, 11, 7, True)
    dibujar_campo(dibujo, "Apellidos", apellidos, 15, 18, 17, 9, True)
    dibujar_campo(dibujo, "Empresa", data.company, 32, 35, 11, 7, True)
    dibujar_campo(dibujo, "Cargo", data.job_title, 44, 47, 13, 8, True)
    dibujar_campo(
        dibujo,
        "Correo",
        str(data.email) if data.email else None,
        58,
        61,
        8,
        5,
        True,
    )
    if incluir_qr:
        codigo_qr = generar_codigo_qr(url_networking(data))
        qr_x = (imagen.width - codigo_qr.width) // 2
        qr_y = imagen.height - codigo_qr.height - mm_a_px(QR_MARGEN_INFERIOR_MM)
        imagen.paste(codigo_qr, (qr_x, qr_y))
    return imagen


def generar_imagen_tira(
    data: Formulario,
    form_id: str,
    position: int,
    configuracion: ConfiguracionTira,
    offset_x_mm: float = 0,
    offset_y_mm: float = 0,
) -> Image.Image:
    """Genera la media hoja Carta y ubica un gafete horizontal en un espacio."""

    if position not in (1, 2, 3):
        raise ValueError("La posicion debe ser 1, 2 o 3")

    tira = Image.new(
        "RGB",
        (
            mm_a_px(configuracion.paper_width_mm),
            mm_a_px(configuracion.paper_height_mm),
        ),
        "white",
    )

    pagina = generar_imagen_impresion(data, form_id, incluir_qr=False)
    ancho_sticker = mm_a_px(STICKER_ANCHO_MM)
    izquierda = (pagina.width - ancho_sticker) // 2
    gafete = pagina.crop((izquierda, 0, izquierda + ancho_sticker, pagina.height))
    gafete = gafete.rotate(90, expand=True)
    gafete = gafete.resize(
        (
            mm_a_px(configuracion.badge_width_mm),
            mm_a_px(configuracion.badge_height_mm),
        ),
        Image.Resampling.LANCZOS,
    )
    padding_izquierdo = mm_a_px(
        padding_formulario_mm(position, configuracion.form_padding_left_mm)
    )
    if padding_izquierdo:
        contenido_desplazado = Image.new("RGB", gafete.size, "white")
        contenido_desplazado.paste(gafete, (padding_izquierdo, 0))
        gafete = contenido_desplazado

    # El QR se agrega despues del padding para que nunca se recorte en la posicion 1.
    codigo_qr = generar_codigo_qr(url_networking(data))
    qr_x = gafete.width - codigo_qr.width - mm_a_px(QR_MARGEN_DERECHO_MM)
    qr_y = (
        (gafete.height - codigo_qr.height) // 2
        - mm_a_px(QR_DESPLAZAMIENTO_ARRIBA_MM)
    )
    gafete.paste(codigo_qr, (qr_x, qr_y))

    separacion = (
        configuracion.paper_height_mm - configuracion.badge_height_mm * 3
    ) / 4
    x_mm = (
        (configuracion.paper_width_mm - configuracion.badge_width_mm) / 2
        + configuracion.global_offset_x_mm
        + offset_x_mm
    )
    posicion_fisica = 4 - position
    y_mm = (
        separacion * posicion_fisica
        + configuracion.badge_height_mm * (posicion_fisica - 1)
        + configuracion.global_offset_y_mm
        + offset_y_mm
    )

    x = mm_a_px(x_mm)
    y = mm_a_px(y_mm)
    if x < 0 or y < 0 or x + gafete.width > tira.width or y + gafete.height > tira.height:
        raise ValueError("Los ajustes desplazan el gafete fuera de la tira")
    tira.paste(gafete, (x, y))
    return tira


def imagen_a_png(imagen: Image.Image) -> bytes:
    contenido = BytesIO()
    imagen.save(contenido, format="PNG", dpi=(DPI_RENDER, DPI_RENDER))
    return contenido.getvalue()


def imprimir_windows(
    imagen: Image.Image,
    printer_name: str | None = None,
    papel_ancho_mm: float = PAPEL_ANCHO_MM,
    papel_alto_mm: float = PAPEL_ALTO_MM,
) -> str:
    nombre_impresora = printer_name or win32print.GetDefaultPrinter()
    nombre_normalizado = nombre_impresora.casefold()
    if "pdf" in nombre_normalizado or "onenote" in nombre_normalizado:
        raise ValueError(
            "Selecciona una impresora fisica; no se admite una impresora virtual"
        )

    dc = win32ui.CreateDC()
    documento_iniciado = False
    try:
        dc.CreatePrinterDC(nombre_impresora)
        dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
        ancho_fisico = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        alto_fisico = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)

        dc.StartDoc("Formulario Nexus")
        documento_iniciado = True
        dc.StartPage()

        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        ancho_destino = min(
            ancho_fisico,
            round(papel_ancho_mm * dpi_x / 25.4),
        )
        alto_destino = min(
            alto_fisico,
            round(papel_alto_mm * dpi_y / 25.4),
        )
        izquierda = (ancho_fisico - ancho_destino) // 2 - offset_x
        arriba = (alto_fisico - alto_destino) // 2 - offset_y
        destino = (
            izquierda,
            arriba,
            izquierda + ancho_destino,
            arriba + alto_destino,
        )
        ImageWin.Dib(imagen).draw(dc.GetHandleOutput(), destino)

        dc.EndPage()
        dc.EndDoc()
        documento_iniciado = False
        return nombre_impresora
    except Exception:
        if documento_iniciado:
            try:
                dc.AbortDoc()
            except Exception:
                pass
        raise
    finally:
        dc.DeleteDC()


@app.get(
    "/",
    tags=["Estado"],
    summary="Comprobar el estado del servicio",
    description="Confirma que la API esta iniciada y puede recibir solicitudes.",
    response_description="Servicio disponible.",
    response_model=EstadoServicio,
    operation_id="comprobar_estado",
)
def root() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "API de impresion de gafetes",
    }


@app.get(
    "/preview",
    tags=["Vistas web"],
    summary="Abrir la pagina de previsualizacion",
    description="Devuelve la interfaz HTML desde la que se puede probar y previsualizar un gafete.",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Pagina HTML de previsualizacion.",
            "content": {"text/html": {"schema": {"type": "string"}}},
        }
    },
    operation_id="abrir_previsualizacion",
)
def vista_previa() -> FileResponse:
    return FileResponse(DIRECTORIO_BASE / "static" / "preview.html")


@app.post(
    "/preview",
    tags=["Impresion"],
    summary="Previsualizar el texto de un gafete",
    description=(
        "Genera una representacion textual simple del orden de los campos, pero no "
        "crea un registro ni imprime. Para revisar la pagina grafica exacta usa el "
        "endpoint print.png de un formulario guardado."
    ),
    response_class=PlainTextResponse,
    responses={
        200: {
            "description": "Representacion textual simplificada del gafete.",
            "content": {
                "text/plain": {
                    "schema": {"type": "string"},
                    "example": "                  Ana\n             Perez Lopez\n                Nexus\n         Gerente comercial",
                }
            },
        },
        422: RESPUESTA_VALIDACION,
    },
    operation_id="previsualizar_gafete",
)
def previsualizar(data: Formulario) -> str:
    return generar_formulario(data)


@app.post(
    "/forms",
    status_code=status.HTTP_201_CREATED,
    tags=["Formularios"],
    summary="Registrar un formulario",
    description=(
        "Guarda los datos del asistente y devuelve las URLs publicas de su pagina "
        "de consulta y de su codigo QR. El campo printer_name no se almacena."
    ),
    response_description="Formulario creado correctamente.",
    response_model=FormularioCreado,
    responses={422: RESPUESTA_VALIDACION},
    operation_id="crear_formulario",
)
def crear_formulario(data: Formulario) -> dict[str, object]:
    form_id, creado = guardar_formulario(data)
    return {
        "ok": True,
        "id": form_id,
        "created_at": creado,
        "view_url": url_formulario(form_id),
        "qr_url": f"{PUBLIC_BASE_URL}/api/forms/{form_id}/qr",
        "print_preview_url": f"{PUBLIC_BASE_URL}/api/forms/{form_id}/print.png",
        "data": data.model_dump(mode="json", exclude={"printer_name"}),
    }


@app.get(
    "/api/forms/{form_id}",
    tags=["Formularios"],
    summary="Consultar un formulario",
    description="Recupera los datos almacenados de un formulario mediante su identificador UUID.",
    response_description="Formulario encontrado.",
    response_model=FormularioConsultado,
    responses={
        404: {
            "model": RespuestaError,
            "description": "No existe un formulario con el identificador indicado.",
        },
        422: RESPUESTA_VALIDACION,
    },
    operation_id="consultar_formulario",
)
def consultar_formulario(
    form_id: Annotated[
        str,
        PathParameter(
            description="Identificador UUID devuelto al crear el formulario.",
            examples=["8a86d334-7f8d-46e4-9652-f6fa585193d8"],
        ),
    ],
) -> dict[str, object]:
    formulario = obtener_formulario(form_id)
    if formulario is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")
    return formulario


@app.get(
    "/api/forms/{form_id}/qr",
    tags=["Formularios"],
    summary="Descargar el codigo QR de un formulario",
    description=(
        "Genera una imagen PNG cuyo codigo QR dirige a la pagina de networking de "
        "Expo Teleinfo con los datos del formulario. El formulario debe existir previamente."
    ),
    responses={
        200: {
            "description": "Imagen PNG del codigo QR.",
            "content": {
                "image/png": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        404: {
            "model": RespuestaError,
            "description": "No existe un formulario con el identificador indicado.",
        },
        422: RESPUESTA_VALIDACION,
    },
    operation_id="obtener_qr_formulario",
)
def qr_formulario(
    form_id: Annotated[
        str,
        PathParameter(
            description="Identificador UUID devuelto al crear el formulario.",
            examples=["8a86d334-7f8d-46e4-9652-f6fa585193d8"],
        ),
    ],
) -> Response:
    registro = obtener_formulario(form_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")
    data = formulario_desde_registro(registro)
    imagen = qrcode.make(url_networking(data))
    contenido = BytesIO()
    imagen.save(contenido, format="PNG")
    return Response(content=contenido.getvalue(), media_type="image/png")


@app.get(
    "/api/forms/{form_id}/print.png",
    tags=["Formularios"],
    summary="Descargar la imagen exacta de impresion",
    description=(
        "Genera a 300 DPI la pagina individual heredada de 109 x 100 mm. No reserva "
        "una posicion ni representa la tira completa; para el flujo actual usa "
        "POST /print y consulta GET /print-state."
    ),
    responses={
        200: {
            "description": "Imagen PNG de impresion a 300 DPI.",
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        404: {
            "model": RespuestaError,
            "description": "No existe un formulario con el identificador indicado.",
        },
        422: RESPUESTA_VALIDACION,
    },
    operation_id="obtener_imagen_impresion",
)
def imagen_impresion_formulario(
    form_id: Annotated[
        str,
        PathParameter(
            description="Identificador UUID devuelto al crear el formulario.",
            examples=["8a86d334-7f8d-46e4-9652-f6fa585193d8"],
        ),
    ],
) -> Response:
    registro = obtener_formulario(form_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")
    data = formulario_desde_registro(registro)
    imagen = generar_imagen_impresion(data, form_id)
    return Response(content=imagen_a_png(imagen), media_type="image/png")


@app.get(
    "/forms/{form_id}",
    tags=["Vistas web"],
    summary="Abrir la vista publica de un formulario",
    description="Devuelve una pagina HTML que carga y muestra los datos del formulario solicitado.",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Pagina HTML del formulario.",
            "content": {"text/html": {"schema": {"type": "string"}}},
        },
        404: {
            "model": RespuestaError,
            "description": "No existe un formulario con el identificador indicado.",
        },
        422: RESPUESTA_VALIDACION,
    },
    operation_id="abrir_formulario",
)
def vista_formulario(
    form_id: Annotated[
        str,
        PathParameter(
            description="Identificador UUID devuelto al crear el formulario.",
            examples=["8a86d334-7f8d-46e4-9652-f6fa585193d8"],
        ),
    ],
) -> FileResponse:
    if obtener_formulario(form_id) is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")
    return FileResponse(DIRECTORIO_BASE / "static" / "form-detail.html")


@app.get(
    "/printers",
    tags=["Impresion"],
    summary="Listar las impresoras disponibles",
    description=(
        "Consulta Windows y devuelve las impresoras locales o conectadas, junto con "
        "la impresora configurada como predeterminada."
    ),
    response_description="Impresoras detectadas.",
    response_model=ListaImpresoras,
    responses={
        503: {
            "model": RespuestaError,
            "description": "Windows no pudo enumerar las impresoras.",
        }
    },
    operation_id="listar_impresoras",
)
def listar_impresoras() -> dict[str, list[str] | str | None]:
    try:
        impresoras = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        nombres = [impresora[2] for impresora in impresoras]
        try:
            predeterminada = win32print.GetDefaultPrinter()
        except Exception:
            predeterminada = None
        return {"printers": nombres, "default": predeterminada}
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudieron consultar las impresoras: {error}",
        ) from error


@app.get(
    "/people",
    tags=["Impresion"],
    summary="Buscar personas pendientes o impresas",
    description=(
        "Consulta en tiempo real la tabla persons de PostgreSQL para alimentar el "
        "buscador de impresion manual."
    ),
    response_model=ListaPersonasParaImpresion,
    responses={503: {"model": RespuestaError, "description": "PostgreSQL no esta disponible."}},
    operation_id="buscar_personas_para_impresion",
)
def buscar_personas_para_impresion(
    search: Annotated[str, Query(max_length=100)] = "",
    status_filter: Annotated[
        Literal["all", "pending", "printed"],
        Query(alias="status"),
    ] = "all",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    try:
        from print_worker import ColaImpresionPostgres, cargar_url_postgres

        cola = ColaImpresionPostgres(cargar_url_postgres())
        personas = cola.buscar_personas(search, status_filter, limit)
        for persona in personas:
            persona["id"] = str(persona["id"])
        return {"people": personas, "count": len(personas)}
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudieron consultar las personas: {error}",
        ) from error


@app.post(
    "/people/{person_id}/print",
    tags=["Impresion"],
    summary="Imprimir manualmente una persona",
    description=(
        "Reserva de forma atomica una persona pendiente o ya impresa, genera su "
        "gafete en la siguiente posicion de la tira y actualiza su estado en PostgreSQL."
    ),
    response_model=ImpresionAutomaticaEnviada,
    responses={
        409: {"model": RespuestaError, "description": "La persona ya esta siendo procesada."},
        503: {"model": RespuestaError, "description": "PostgreSQL no esta disponible."},
    },
    operation_id="imprimir_persona_manualmente",
)
def imprimir_persona_manualmente(
    person_id: Annotated[str, PathParameter(description="Identificador de persons.")],
    solicitud: ImpresionManualPersona,
) -> dict[str, bool | int | str | None]:
    from print_worker import (
        ColaImpresionPostgres,
        cargar_url_postgres,
        descripcion_error,
        formulario_desde_persona,
    )

    try:
        cola = ColaImpresionPostgres(cargar_url_postgres())
        persona = cola.reclamar_persona(person_id)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo reservar la persona: {error}",
        ) from error

    if persona is None:
        raise HTTPException(
            status_code=409,
            detail="La persona no existe, esta inactiva o ya esta siendo procesada",
        )

    formulario = formulario_desde_persona(persona, solicitud.printer_name)
    claimed_person_id = persona["id"]
    try:
        resultado = procesar_impresion(formulario, simulate=solicitud.simulate)
    except Exception as error:
        try:
            cola.marcar_fallida(claimed_person_id, descripcion_error(error))
        except Exception:
            pass
        raise

    try:
        cola.marcar_impresa(claimed_person_id)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "El trabajo se proceso, pero no se pudo confirmar como impreso en "
                f"PostgreSQL: {error}"
            ),
        ) from error
    return resultado


@app.get(
    "/bluetooth/ports",
    tags=["Impresion"],
    summary="Listar puertos serie para impresoras Bluetooth",
    description=(
        "Devuelve los puertos COM visibles en Windows. Luego de emparejar la PT-210, "
        "su puerto serie Bluetooth debe aparecer en esta lista."
    ),
    response_description="Puertos serie detectados.",
    response_model=ListaPuertosSeriales,
    responses={
        503: {
            "model": RespuestaError,
            "description": "Windows no pudo enumerar los puertos serie.",
        }
    },
    operation_id="listar_puertos_bluetooth",
)
def listar_puertos_bluetooth() -> dict[str, object]:
    try:
        return {"ports": listar_puertos_seriales()}
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudieron consultar los puertos serie: {error}",
        ) from error


@app.get(
    "/print-layout",
    tags=["Impresion"],
    summary="Consultar la configuracion de la tira",
    description=(
        "Devuelve las medidas persistidas de la media hoja Carta, de cada cuadro y "
        "de la calibracion general aplicada a las tres posiciones."
    ),
    response_model=ConfiguracionTira,
    operation_id="consultar_configuracion_tira",
)
def consultar_configuracion_tira() -> ConfiguracionTira:
    return obtener_configuracion_tira()


@app.put(
    "/print-layout",
    tags=["Impresion"],
    summary="Ajustar la configuracion de la tira",
    description=(
        "Guarda las medidas y los desplazamientos generales en milimetros. "
        "Los tres gafetes deben caber dentro de la tira."
    ),
    response_model=ConfiguracionTira,
    responses={422: RESPUESTA_VALIDACION},
    operation_id="ajustar_configuracion_tira",
)
def ajustar_configuracion_tira(
    configuracion: ConfiguracionTira,
) -> ConfiguracionTira:
    return guardar_configuracion_tira(configuracion)


@app.get(
    "/print-state",
    tags=["Impresion"],
    summary="Consultar la tira activa y su siguiente posicion",
    description=(
        "Devuelve la tira activa o la ultima tira completada y el formulario asignado "
        "a la unica posicion habilitada: la 2."
    ),
    response_model=EstadoPosiciones,
    operation_id="consultar_estado_posiciones",
)
def consultar_estado_posiciones() -> dict[str, object]:
    return obtener_estado_posiciones()


@app.put(
    "/print-state",
    tags=["Impresion"],
    summary="Corregir la siguiente posicion o iniciar otra tira",
    description=(
        "Permite corregir manualmente la proxima posicion. Con start_new_strip=true "
        "abandona la tira activa y comienza una nueva."
    ),
    response_model=EstadoPosiciones,
    responses={422: RESPUESTA_VALIDACION},
    operation_id="ajustar_estado_posiciones",
)
def configurar_estado_posiciones(
    ajuste: AjusteEstadoPosiciones,
) -> dict[str, object]:
    return ajustar_estado_posiciones(ajuste)


@app.post(
    "/api/forms/{form_id}/print-position",
    tags=["Impresion"],
    summary="Imprimir un formulario en una posicion de la tira",
    description=(
        "Genera una pagina completa para media hoja Carta y coloca el formulario "
        "guardado exclusivamente en la posicion 2. Los desplazamientos permiten corregir "
        "variaciones de alimentacion para una impresion concreta."
    ),
    response_model=ImpresionPosicionEnviada,
    responses={
        400: {
            "model": RespuestaError,
            "description": "El gafete queda fuera de la tira o el papel no coincide.",
        },
        404: {
            "model": RespuestaError,
            "description": "No existe el formulario indicado.",
        },
        422: RESPUESTA_VALIDACION,
        500: {
            "model": RespuestaError,
            "description": "No se pudo enviar el trabajo al controlador de Windows.",
        },
    },
    operation_id="imprimir_formulario_en_posicion",
)
def imprimir_formulario_en_posicion(
    form_id: Annotated[
        str,
        PathParameter(description="Identificador del formulario guardado."),
    ],
    ajuste: ImpresionPosicion,
) -> dict[str, bool | float | int | str]:
    registro = obtener_formulario(form_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")

    try:
        configuracion = obtener_configuracion_tira()
        data = formulario_desde_registro(registro)
        imagen = generar_imagen_tira(
            data,
            form_id,
            POSICION_IMPRESION,
            configuracion,
            ajuste.offset_x_mm,
            ajuste.offset_y_mm,
        )
        impresora = imprimir_windows(
            imagen,
            ajuste.printer_name,
            configuracion.paper_width_mm,
            configuracion.paper_height_mm,
        )
        return {
            "ok": True,
            "message": f"Impresion enviada a la posicion {POSICION_IMPRESION}",
            "printer": impresora,
            "id": form_id,
            "position": POSICION_IMPRESION,
            "offset_x_mm": ajuste.offset_x_mm,
            "offset_y_mm": ajuste.offset_y_mm,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo imprimir: {error}",
        ) from error


@app.post(
    "/print",
    tags=["Impresion"],
    summary="Asignar e imprimir automaticamente un gafete",
    description=(
        "Guarda el formulario y reserva atomicamente la posicion 2 de una tira nueva. "
        "Las posiciones 1 y 3 se ignoran. Por defecto "
        "simulate=true evita el acceso a la impresora; usa simulate=false para enviar "
        "el trabajo al controlador de Windows."
    ),
    response_description="Posicion reservada y trabajo simulado o enviado.",
    response_model=ImpresionAutomaticaEnviada,
    responses={
        422: RESPUESTA_VALIDACION,
        400: {
            "model": RespuestaError,
            "description": "La impresora seleccionada es virtual.",
        },
        500: {
            "model": RespuestaError,
            "description": "No se pudo abrir la impresora o enviar el trabajo.",
        },
    },
    operation_id="imprimir_gafete",
)
def imprimir(
    data: Formulario,
    simulate: Annotated[
        bool,
        Query(
            description=(
                "true registra y genera la simulacion sin usar Windows; false envia "
                "la tira al controlador de la impresora."
            ),
            examples=[True],
        ),
    ] = True,
) -> dict[str, bool | int | str | None]:
    return procesar_impresion(data, simulate=simulate)


def procesar_impresion(
    data: Formulario,
    simulate: bool = True,
    transport: Literal["windows", "bluetooth"] = "windows",
    bluetooth_port: str | None = None,
    bluetooth_baudrate: int = DEFAULT_BAUDRATE,
) -> dict[str, bool | int | str | None]:
    """Punto de entrada compartido por HTTP y el worker de PostgreSQL."""

    job_id: str | None = None
    try:
        form_id, _ = guardar_formulario(data)
        job_id, strip_id, position, siguiente, completa = reservar_posicion(form_id)
        if transport == "windows":
            configuracion = obtener_configuracion_tira()
            imagen = generar_imagen_tira(
                data,
                form_id,
                position,
                configuracion,
            )
        elif transport == "bluetooth":
            configuracion = None
            imagen = generar_imagen_termica(data)
            if not bluetooth_port:
                raise ValueError(
                    "Indica el puerto COM asignado a la impresora Bluetooth"
                )
        else:
            raise ValueError("El transporte debe ser windows o bluetooth")

        impresora = None
        if simulate:
            actualizar_trabajo_impresion(job_id, "simulated")
        elif transport == "windows":
            assert configuracion is not None
            impresora = imprimir_windows(
                imagen,
                data.printer_name,
                configuracion.paper_width_mm,
                configuracion.paper_height_mm,
            )
            actualizar_trabajo_impresion(job_id, "sent")
        else:
            impresora = enviar_bluetooth(
                imagen,
                bluetooth_port,
                bluetooth_baudrate,
            )
            actualizar_trabajo_impresion(job_id, "sent")
        return {
            "ok": True,
            "message": (
                f"Simulacion asignada a la posicion {position}"
                if simulate
                else f"Impresion enviada a la posicion {position}"
            ),
            "simulated": simulate,
            "printer": impresora,
            "id": form_id,
            "strip_id": strip_id,
            "position": position,
            "next_position": siguiente,
            "strip_completed": completa,
            "view_url": url_formulario(form_id),
        }
    except ValueError as error:
        if job_id:
            fallar_trabajo_y_devolver_posicion(job_id, str(error))
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        if job_id:
            fallar_trabajo_y_devolver_posicion(job_id, str(error))
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo imprimir: {error}",
        ) from error


@app.post(
    "/print/bluetooth",
    tags=["Impresion"],
    summary="Imprimir un gafete en una termica Bluetooth",
    description=(
        "Renderiza los mismos datos del gafete para la GOOJPRT PT-210 de 48 mm y "
        "los envia como imagen ESC/POS al puerto COM Bluetooth asignado por Windows."
    ),
    response_description="Trabajo termico simulado o enviado por Bluetooth.",
    response_model=ImpresionAutomaticaEnviada,
    responses={
        400: {
            "model": RespuestaError,
            "description": "El puerto COM o los parametros son invalidos.",
        },
        422: RESPUESTA_VALIDACION,
        500: {
            "model": RespuestaError,
            "description": "No se pudo abrir el puerto o enviar ESC/POS.",
        },
    },
    operation_id="imprimir_gafete_bluetooth",
)
def imprimir_gafete_bluetooth(
    data: Formulario,
    port: Annotated[
        str,
        Query(
            pattern=r"^COM\d+$",
            description="Puerto serie Bluetooth asignado por Windows, por ejemplo COM5.",
        ),
    ],
    baudrate: Annotated[
        int,
        Query(ge=1200, le=921600, description="Velocidad del puerto serie."),
    ] = DEFAULT_BAUDRATE,
    simulate: Annotated[
        bool,
        Query(description="Si es true, renderiza y reserva sin enviar al puerto COM."),
    ] = True,
) -> dict[str, bool | int | str | None]:
    return procesar_impresion(
        data,
        simulate=simulate,
        transport="bluetooth",
        bluetooth_port=port,
        bluetooth_baudrate=baudrate,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9101)
