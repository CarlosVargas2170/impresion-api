from pathlib import Path
from time import sleep
from typing import Any

from PIL import Image, ImageDraw, ImageFont


THERMAL_WIDTH_DOTS = 384
THERMAL_PRINT_WIDTH_MM = 48
THERMAL_DPI = 203
DEFAULT_BAUDRATE = 9600
FUENTE_REGULAR = Path("C:/Windows/Fonts/arial.ttf")
FUENTE_NEGRITA = Path("C:/Windows/Fonts/arialbd.ttf")


def _fuente(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FUENTE_NEGRITA if bold else FUENTE_REGULAR
    return ImageFont.truetype(str(path), size)


def _ajustar_fuente(
    draw: ImageDraw.ImageDraw,
    text: str,
    initial_size: int,
    minimum_size: int,
    max_width: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont:
    for size in range(initial_size, minimum_size - 1, -1):
        font = _fuente(size, bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    return _fuente(minimum_size, bold)


def _dibujar_centrado(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    height = box[3] - box[1]
    draw.text(((THERMAL_WIDTH_DOTS - width) // 2, y - box[1]), text, font=font, fill=0)
    return y + height


def generar_imagen_termica(data: Any) -> Image.Image:
    """Renderiza los campos actuales como una etiqueta monocroma de 384 puntos."""

    canvas = Image.new("L", (THERMAL_WIDTH_DOTS, 700), 255)
    draw = ImageDraw.Draw(canvas)
    max_width = THERMAL_WIDTH_DOTS - 24
    y = 18

    first_name = str(data.first_name)
    first_font = _ajustar_fuente(draw, first_name, 43, 25, max_width, True)
    y = _dibujar_centrado(draw, first_name, y, first_font) + 7

    surnames = " ".join(
        value
        for value in (data.paternal_surname, data.maternal_surname)
        if value
    )
    surname_font = _ajustar_fuente(draw, surnames, 34, 21, max_width, True)
    y = _dibujar_centrado(draw, surnames, y, surname_font) + 18

    fields = (
        ("EMPRESA", data.company, 27),
        ("CARGO", data.job_title, 25),
        ("CORREO", str(data.email) if data.email else None, 22),
    )
    label_font = _fuente(16)
    for label, value, initial_size in fields:
        if not value:
            continue
        y = _dibujar_centrado(draw, label, y, label_font) + 4
        value = str(value)
        value_font = _ajustar_fuente(
            draw,
            value,
            initial_size,
            16,
            max_width,
            True,
        )
        y = _dibujar_centrado(draw, value, y, value_font) + 16

    bottom = min(canvas.height, y + 24)
    return canvas.crop((0, 0, THERMAL_WIDTH_DOTS, bottom))


def imagen_a_escpos(image: Image.Image) -> bytes:
    """Convierte una imagen al comando raster GS v 0 de ESC/POS."""

    monochrome = image.convert("L").point(lambda pixel: 0 if pixel < 160 else 255, "1")
    if monochrome.width > THERMAL_WIDTH_DOTS:
        raise ValueError(
            f"La imagen supera los {THERMAL_WIDTH_DOTS} puntos de la PT-210"
        )
    if monochrome.width % 8:
        padded_width = ((monochrome.width + 7) // 8) * 8
        padded = Image.new("1", (padded_width, monochrome.height), 1)
        padded.paste(monochrome, (0, 0))
        monochrome = padded

    bytes_per_row = monochrome.width // 8
    raster = bytearray()
    pixels = monochrome.load()
    for y in range(monochrome.height):
        for byte_x in range(bytes_per_row):
            value = 0
            for bit in range(8):
                if pixels[byte_x * 8 + bit, y] == 0:
                    value |= 1 << (7 - bit)
            raster.append(value)

    width_low, width_high = bytes_per_row & 0xFF, bytes_per_row >> 8
    height_low, height_high = monochrome.height & 0xFF, monochrome.height >> 8
    return (
        b"\x1b\x40"
        + bytes((0x1D, 0x76, 0x30, 0, width_low, width_high, height_low, height_high))
        + bytes(raster)
        + b"\n\n\n"
    )


def listar_puertos_seriales() -> list[dict[str, str | None]]:
    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise RuntimeError(
            "Falta pyserial; instala las dependencias de requirements.txt"
        ) from error

    return [
        {
            "device": port.device,
            "description": port.description or None,
            "hwid": port.hwid or None,
        }
        for port in list_ports.comports()
    ]


def imprimir_bluetooth(
    image: Image.Image,
    port: str,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout: float = 10,
) -> str:
    try:
        import serial
    except ImportError as error:
        raise RuntimeError(
            "Falta pyserial; instala las dependencias de requirements.txt"
        ) from error

    normalized_port = port.strip().upper()
    if not normalized_port.startswith("COM") or not normalized_port[3:].isdigit():
        raise ValueError("El puerto Bluetooth debe tener formato COM seguido de un numero")

    payload = imagen_a_escpos(image)
    with serial.Serial(
        port=normalized_port,
        baudrate=baudrate,
        timeout=timeout,
        write_timeout=timeout,
    ) as connection:
        connection.reset_output_buffer()
        for start in range(0, len(payload), 512):
            connection.write(payload[start : start + 512])
            sleep(0.01)
        connection.flush()
    return normalized_port
