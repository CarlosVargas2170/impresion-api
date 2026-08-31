import json
import os
import sqlite3
from contextlib import closing
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import qrcode
import win32con
import win32print
import win32ui
from fastapi import FastAPI, HTTPException, Path as PathParameter, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from PIL import Image, ImageDraw, ImageFont, ImageWin
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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
    version="1.0.0",
    description=(
        "API para registrar asistentes, consultar sus formularios, generar codigos QR "
        "y enviar gafetes en formato ESC/POS a una impresora termica de Windows.\n\n"
        "La documentacion interactiva permite probar todos los endpoints. Los campos "
        "no definidos en el contrato son rechazados y las fechas deben enviarse en "
        "formato ISO 8601."
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
        configuracion.get("public_base_url", "http://127.0.0.1:9102"),
    )
    return url.rstrip("/")


PUBLIC_BASE_URL = cargar_url_publica()

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
        return value

    @field_validator(
        "maternal_surname",
        "description",
        "company",
        "job_title",
        "phone_prefix",
        "phone_number",
        "email",
        "printer_name",
        mode="before",
    )
    @classmethod
    def limpiar_opcional(cls, value: object) -> object:
        if not isinstance(value, str):
            return None
        return value.strip() or None


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


class ImpresionEnviada(BaseModel):
    ok: bool = Field(description="Indica que el trabajo fue aceptado por Windows.")
    message: str = Field(description="Resultado legible de la operacion.")
    printer: str = Field(description="Nombre de la impresora que recibio el trabajo.")
    id: str = Field(description="Identificador UUID del formulario guardado.")
    view_url: str = Field(description="URL publica de la vista HTML del formulario.")
    qr_url: str = Field(description="URL publica de la imagen del codigo QR.")
    print_preview_url: str = Field(description="URL de la imagen exacta enviada a imprimir.")


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


def formulario_desde_registro(registro: dict[str, object]) -> Formulario:
    datos = {
        campo: registro.get(campo)
        for campo in Formulario.model_fields
        if campo != "printer_name"
    }
    return Formulario.model_validate(datos)


inicializar_db()


# Medidas del soporte individual y del sticker, en milimetros.
ANCHO = 40
PAPEL_ANCHO_MM = 109
PAPEL_ALTO_MM = 100
STICKER_ANCHO_MM = 85
DPI_RENDER = 300
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
    fuente_etiqueta = cargar_fuente(False, 7)
    fuente_valor = fuente_ajustada(
        dibujo, valor, puntos, minimo, ancho_util, negrita
    )
    dibujar_centrado(
        dibujo, etiqueta.upper(), y_etiqueta_mm, fuente_etiqueta, "#6b7774"
    )
    dibujar_centrado(dibujo, valor, y_valor_mm, fuente_valor, "#17201f")


def generar_imagen_impresion(data: Formulario, form_id: str) -> Image.Image:
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

    dibujar_campo(dibujo, "Nombre", data.first_name, 4.5, 7.5, 27, 15, True)
    dibujar_campo(dibujo, "Apellidos", apellidos, 19, 22, 18, 11, True)
    dibujar_campo(dibujo, "Empresa", data.company, 32.5, 35.5, 14, 9, True)
    dibujar_campo(dibujo, "Cargo", data.job_title, 43.5, 46.5, 12, 8, True)
    dibujar_campo(
        dibujo,
        "Correo",
        str(data.email) if data.email else None,
        55,
        58,
        11,
        7,
        True,
    )

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url_formulario(form_id))
    qr.make(fit=True)
    imagen_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    lado_qr = mm_a_px(25)
    imagen_qr = imagen_qr.resize((lado_qr, lado_qr), Image.Resampling.NEAREST)
    x_qr = (imagen.width - lado_qr) // 2
    imagen.paste(imagen_qr, (x_qr, mm_a_px(70)))
    return imagen


def imagen_a_png(imagen: Image.Image) -> bytes:
    contenido = BytesIO()
    imagen.save(contenido, format="PNG", dpi=(DPI_RENDER, DPI_RENDER))
    return contenido.getvalue()


def imprimir_windows(imagen: Image.Image, printer_name: str | None = None) -> str:
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
        ancho_mm = ancho_fisico * 25.4 / dpi_x
        alto_mm = alto_fisico * 25.4 / dpi_y
        if abs(ancho_mm - PAPEL_ANCHO_MM) > 5 or abs(alto_mm - PAPEL_ALTO_MM) > 5:
            raise ValueError(
                "El controlador reporta papel "
                f"{ancho_mm:.0f} x {alto_mm:.0f} mm; configura "
                f"{PAPEL_ANCHO_MM} x {PAPEL_ALTO_MM} mm en orientacion vertical"
            )

        dc.StartDoc("Formulario Nexus")
        documento_iniciado = True
        dc.StartPage()

        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        destino = (
            -offset_x,
            -offset_y,
            ancho_fisico - offset_x,
            alto_fisico - offset_y,
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
        "Genera una imagen PNG cuyo codigo QR dirige a la vista publica del formulario. "
        "El formulario debe existir previamente."
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
    if obtener_formulario(form_id) is None:
        raise HTTPException(status_code=404, detail="Formulario no encontrado")
    imagen = qrcode.make(url_formulario(form_id))
    contenido = BytesIO()
    imagen.save(contenido, format="PNG")
    return Response(content=contenido.getvalue(), media_type="image/png")


@app.get(
    "/api/forms/{form_id}/print.png",
    tags=["Formularios"],
    summary="Descargar la imagen exacta de impresion",
    description=(
        "Genera a 300 DPI la pagina grafica de 109 x 100 mm que se enviara al "
        "controlador de la impresora. No crea ningun trabajo de impresion."
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


@app.post(
    "/print",
    tags=["Impresion"],
    summary="Imprimir un gafete",
    description=(
        "Guarda el formulario, genera una pagina grafica de 109 x 100 mm con su QR "
        "y la envia mediante el controlador grafico de Windows. El controlador debe "
        "estar configurado con ese mismo papel en orientacion vertical. Una respuesta "
        "exitosa confirma el envio a Windows, no necesariamente la impresion fisica."
    ),
    response_description="Trabajo enviado a la impresora.",
    response_model=ImpresionEnviada,
    responses={
        422: RESPUESTA_VALIDACION,
        400: {
            "model": RespuestaError,
            "description": "La impresora es virtual o tiene otro tamaño de papel.",
        },
        500: {
            "model": RespuestaError,
            "description": "No se pudo abrir la impresora o enviar el trabajo.",
        },
    },
    operation_id="imprimir_gafete",
)
def imprimir(data: Formulario) -> dict[str, bool | str]:
    try:
        form_id, _ = guardar_formulario(data)
        imagen = generar_imagen_impresion(data, form_id)
        impresora = imprimir_windows(imagen, data.printer_name)
        return {
            "ok": True,
            "message": "Impresion enviada",
            "printer": impresora,
            "id": form_id,
            "view_url": url_formulario(form_id),
            "qr_url": f"{PUBLIC_BASE_URL}/api/forms/{form_id}/qr",
            "print_preview_url": f"{PUBLIC_BASE_URL}/api/forms/{form_id}/print.png",
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo imprimir: {error}",
        ) from error


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9101)
