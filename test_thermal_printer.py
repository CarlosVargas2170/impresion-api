import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageChops

import app as api
import thermal_printer as thermal
from app import Formulario


def formulario_prueba() -> Formulario:
    return Formulario(
        first_name="Alejandra",
        paternal_surname="Fernandez",
        maternal_surname="Mamani",
        company="Nexus Eventos",
        job_title="Coordinadora comercial",
        email="alejandra.fernandez@ejemplo.com",
    )


class RenderTermicoTests(unittest.TestCase):
    def test_renderiza_ancho_nativo_y_contenido(self):
        image = thermal.generar_imagen_termica(formulario_prueba())
        ink = ImageChops.invert(image).getbbox()

        self.assertEqual(image.width, 384)
        self.assertEqual(image.mode, "L")
        self.assertIsNotNone(ink)

    def test_convierte_imagen_a_raster_escpos(self):
        image = Image.new("1", (384, 10), 1)
        image.putpixel((0, 0), 0)

        payload = thermal.imagen_a_escpos(image)

        self.assertTrue(payload.startswith(b"\x1b\x40\x1d\x76\x30\x00"))
        self.assertEqual(payload[6] + payload[7] * 256, 48)
        self.assertEqual(payload[8] + payload[9] * 256, 10)
        self.assertEqual(payload[10], 0b10000000)
        self.assertTrue(payload.endswith(b"\n\n\n"))

    @patch("serial.Serial")
    def test_envia_escpos_al_puerto_com(self, serial_class):
        connection = MagicMock()
        serial_class.return_value.__enter__.return_value = connection

        result = thermal.imprimir_bluetooth(
            Image.new("1", (384, 2), 1),
            "com7",
            baudrate=9600,
        )

        self.assertEqual(result, "COM7")
        serial_class.assert_called_once_with(
            port="COM7",
            baudrate=9600,
            timeout=10,
            write_timeout=10,
        )
        connection.flush.assert_called_once_with()
        self.assertGreaterEqual(connection.write.call_count, 1)

    def test_rechaza_puerto_no_serial(self):
        with self.assertRaisesRegex(ValueError, "formato COM"):
            thermal.imprimir_bluetooth(Image.new("1", (384, 2), 1), "PT-210")


class EndpointBluetoothTests(unittest.TestCase):
    @patch("app.enviar_bluetooth", return_value="COM7")
    def test_imprime_con_el_flujo_actual_por_bluetooth(self, enviar):
        with tempfile.TemporaryDirectory() as temporal:
            directory = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directory),
                patch.object(api, "RUTA_DB", directory / "forms.db"),
            ):
                api.inicializar_db()
                result = api.imprimir_gafete_bluetooth(
                    formulario_prueba(),
                    port="COM7",
                    baudrate=9600,
                    simulate=False,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["printer"], "COM7")
        image, port, baudrate = enviar.call_args.args
        self.assertEqual(image.width, 384)
        self.assertEqual(port, "COM7")
        self.assertEqual(baudrate, 9600)

    @patch("app.listar_puertos_seriales")
    def test_lista_puertos_bluetooth(self, listar):
        listar.return_value = [
            {"device": "COM7", "description": "Bluetooth", "hwid": "BTHENUM"}
        ]

        self.assertEqual(api.listar_puertos_bluetooth()["ports"][0]["device"], "COM7")


if __name__ == "__main__":
    unittest.main()
