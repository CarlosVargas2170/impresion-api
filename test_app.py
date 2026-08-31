import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

import app as api
from app import (
    Formulario,
    generar_formulario,
    generar_imagen_impresion,
    imagen_a_png,
    imprimir_windows,
    previsualizar,
)


class FormularioTests(unittest.TestCase):
    def datos_validos(self, **cambios):
        datos = {
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
        datos.update(cambios)
        return datos

    def test_genera_todos_los_campos(self):
        texto = generar_formulario(Formulario(**self.datos_validos()))

        lineas = texto.splitlines()
        self.assertEqual(lineas[0].strip(), "Ana")
        self.assertEqual(lineas[1].strip(), "Perez Lopez")
        self.assertEqual(lineas[2].strip(), "Nexus")
        self.assertEqual(lineas[3].strip(), "Gerente comercial")
        self.assertEqual(lineas[4], "")
        self.assertEqual(lineas[5].strip(), "ana.perez@ejemplo.com")
        self.assertNotIn("NOMBRE", texto)
        self.assertNotIn("EMPRESA", texto)

    def test_campos_opcionales_aceptan_vacio(self):
        formulario = Formulario(
            **self.datos_validos(
                maternal_surname="  ",
                description="",
                company=None,
                job_title="",
                phone_prefix="",
                phone_number="",
                email="",
                consent_at=None,
            )
        )
        texto = generar_formulario(formulario)

        self.assertIsNone(formulario.maternal_surname)
        self.assertEqual(texto.splitlines()[1].strip(), "Perez")
        self.assertNotIn("Nexus", texto)

    def test_rechaza_correo_invalido(self):
        with self.assertRaises(ValidationError):
            Formulario(**self.datos_validos(email="correo-invalido"))

    def test_rechaza_campos_vacios(self):
        with self.assertRaises(ValidationError):
            Formulario(**self.datos_validos(first_name="   "))

    def test_rechaza_numero_telefonico_invalido(self):
        with self.assertRaises(ValidationError):
            Formulario(**self.datos_validos(phone_number="12345"))

        with self.assertRaises(ValidationError):
            Formulario(**self.datos_validos(phone_number="7123ABC"))

    def test_previsualizacion_usa_el_mismo_formato(self):
        formulario = Formulario(**self.datos_validos())

        self.assertEqual(previsualizar(formulario), generar_formulario(formulario))

    def test_genera_imagen_de_109_por_100_mm_a_300_dpi(self):
        imagen = generar_imagen_impresion(
            Formulario(**self.datos_validos()),
            "formulario-de-prueba",
        )

        self.assertEqual(imagen.size, (api.mm_a_px(109), api.mm_a_px(100)))
        self.assertTrue(imagen.getbbox())
        self.assertTrue(imagen_a_png(imagen).startswith(b"\x89PNG"))


class ImpresionTests(unittest.TestCase):
    def imagen_prueba(self):
        return generar_imagen_impresion(
            Formulario(**FormularioTests().datos_validos()),
            "formulario-de-prueba",
        )

    def test_rechaza_impresora_virtual(self):
        with self.assertRaisesRegex(ValueError, "impresora fisica"):
            imprimir_windows(self.imagen_prueba(), "Microsoft Print to PDF")

    @patch("app.ImageWin.Dib")
    @patch("app.win32ui.CreateDC")
    def test_envia_imagen_al_controlador(self, crear_dc, dib):
        dc = MagicMock()
        crear_dc.return_value = dc

        capacidades = {
            api.win32con.LOGPIXELSX: 300,
            api.win32con.LOGPIXELSY: 300,
            api.win32con.PHYSICALWIDTH: api.mm_a_px(109),
            api.win32con.PHYSICALHEIGHT: api.mm_a_px(100),
            api.win32con.PHYSICALOFFSETX: 0,
            api.win32con.PHYSICALOFFSETY: 0,
        }
        dc.GetDeviceCaps.side_effect = capacidades.__getitem__
        dc.GetHandleOutput.return_value = "handle"

        resultado = imprimir_windows(self.imagen_prueba(), "Epson L3310")

        self.assertEqual(resultado, "Epson L3310")
        dc.CreatePrinterDC.assert_called_once_with("Epson L3310")
        dc.StartDoc.assert_called_once_with("Formulario Nexus")
        dc.StartPage.assert_called_once_with()
        dib.return_value.draw.assert_called_once()
        dc.EndPage.assert_called_once_with()
        dc.EndDoc.assert_called_once_with()
        dc.DeleteDC.assert_called_once_with()


class AlmacenamientoTests(unittest.TestCase):
    def test_guarda_consulta_y_genera_qr(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                form_id, creado = api.guardar_formulario(
                    Formulario(**FormularioTests().datos_validos())
                )

                formulario = api.obtener_formulario(form_id)
                self.assertIsNotNone(formulario)
                self.assertEqual(formulario["first_name"], "Ana")
                self.assertEqual(formulario["created_at"], creado)

                respuesta = api.qr_formulario(form_id)
                self.assertEqual(respuesta.media_type, "image/png")
                self.assertTrue(respuesta.body.startswith(b"\x89PNG"))


class DocumentacionOpenAPITests(unittest.TestCase):
    def test_documenta_todos_los_endpoints(self):
        esquema = api.app.openapi()
        operaciones = {
            (metodo.upper(), ruta)
            for ruta, metodos in esquema["paths"].items()
            for metodo in metodos
            if metodo in {"get", "post", "put", "patch", "delete"}
        }

        self.assertEqual(
            operaciones,
            {
                ("GET", "/"),
                ("GET", "/preview"),
                ("POST", "/preview"),
                ("POST", "/forms"),
                ("GET", "/api/forms/{form_id}"),
                ("GET", "/api/forms/{form_id}/qr"),
                ("GET", "/api/forms/{form_id}/print.png"),
                ("GET", "/forms/{form_id}"),
                ("GET", "/printers"),
                ("POST", "/print"),
            },
        )

        for metodo, ruta in operaciones:
            operacion = esquema["paths"][ruta][metodo.lower()]
            self.assertTrue(operacion["summary"])
            self.assertTrue(operacion["description"])
            self.assertTrue(operacion["responses"])

    def test_documenta_formatos_especiales(self):
        esquema = api.app.openapi()

        respuesta_qr = esquema["paths"]["/api/forms/{form_id}/qr"]["get"][
            "responses"
        ]["200"]
        self.assertIn("image/png", respuesta_qr["content"])

        respuesta_impresion = esquema["paths"][
            "/api/forms/{form_id}/print.png"
        ]["get"]["responses"]["200"]
        self.assertIn("image/png", respuesta_impresion["content"])

        respuesta_preview = esquema["paths"]["/preview"]["post"]["responses"][
            "200"
        ]
        self.assertIn("text/plain", respuesta_preview["content"])


if __name__ == "__main__":
    unittest.main()
