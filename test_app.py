import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from PIL import Image, ImageChops

import app as api
from app import (
    ConfiguracionTira,
    Formulario,
    ImpresionPosicion,
    generar_formulario,
    generar_imagen_impresion,
    generar_imagen_tira,
    imagen_a_png,
    imprimir_windows,
    previsualizar,
    url_networking,
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

    @patch("app.generar_codigo_qr", return_value=Image.new("RGB", (20, 20), "black"))
    def test_imagen_de_impresion_incluye_qr(self, generar_qr):
        generar_imagen_impresion(
            Formulario(**self.datos_validos()),
            "formulario-de-prueba",
        )

        generar_qr.assert_called_once_with(
            "https://www.expoteleinfo.com/networking?"
            "nombre=Ana&apellido=Perez+Lopez&telefono=%2B59171234567&"
            "email=ana.perez%40ejemplo.com&cargo=Gerente+comercial&empresa=Nexus"
        )

    def test_url_networking_usa_y_codifica_los_valores_del_formulario(self):
        formulario = Formulario(
            **self.datos_validos(
                first_name="María José",
                paternal_surname="Núñez",
                maternal_surname="D'Angelo",
                company="Nexus & Asociados",
                job_title="I+D",
            )
        )

        self.assertEqual(
            url_networking(formulario),
            "https://www.expoteleinfo.com/networking?"
            "nombre=Mar%C3%ADa+Jos%C3%A9&apellido=N%C3%BA%C3%B1ez+D%27Angelo&"
            "telefono=%2B59171234567&email=ana.perez%40ejemplo.com&"
            "cargo=I%2BD&empresa=Nexus+%26+Asociados",
        )

    def test_url_networking_incluye_campos_opcionales_vacios(self):
        formulario = Formulario(
            **self.datos_validos(
                maternal_surname=None,
                company=None,
                job_title=None,
                phone_prefix=None,
                phone_number=None,
                email=None,
            )
        )

        self.assertEqual(
            url_networking(formulario),
            "https://www.expoteleinfo.com/networking?"
            "nombre=Ana&apellido=Perez&telefono=&email=&cargo=&empresa=",
        )

    def test_codigo_qr_tiene_el_tamano_configurado(self):
        imagen = api.generar_codigo_qr("https://example.com/forms/123")

        self.assertEqual(
            imagen.size,
            (api.mm_a_px(api.QR_TAMANO_MM), api.mm_a_px(api.QR_TAMANO_MM)),
        )


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

    @patch("app.ImageWin.Dib")
    @patch("app.win32ui.CreateDC")
    def test_acepta_controlador_configurado_en_carta_completa(self, crear_dc, dib):
        dc = MagicMock()
        crear_dc.return_value = dc
        capacidades = {
            api.win32con.LOGPIXELSX: 300,
            api.win32con.LOGPIXELSY: 300,
            api.win32con.PHYSICALWIDTH: api.mm_a_px(216),
            api.win32con.PHYSICALHEIGHT: api.mm_a_px(279.4),
            api.win32con.PHYSICALOFFSETX: 0,
            api.win32con.PHYSICALOFFSETY: 0,
        }
        dc.GetDeviceCaps.side_effect = capacidades.__getitem__
        dc.GetHandleOutput.return_value = "handle"

        resultado = imprimir_windows(
            self.imagen_prueba(),
            "EPSON L3310 Series",
            107.95,
            279.4,
        )

        self.assertEqual(resultado, "EPSON L3310 Series")
        dc.StartDoc.assert_called_once_with("Formulario Nexus")
        dib.return_value.draw.assert_called_once()
        destino = dib.return_value.draw.call_args.args[1]
        self.assertGreater(destino[0], 0)
        self.assertAlmostEqual(
            destino[2] - destino[0],
            api.mm_a_px(107.95),
            delta=1,
        )
        dc.EndDoc.assert_called_once_with()


class ImpresionTiraTests(unittest.TestCase):
    def formulario(self):
        return Formulario(**FormularioTests().datos_validos())

    def contenido_impreso(self, imagen):
        fondo = Image.new("RGB", imagen.size, "white")
        return ImageChops.difference(imagen, fondo).getbbox()

    def test_genera_media_carta_con_tres_posiciones_distintas(self):
        configuracion = ConfiguracionTira()
        imagenes = [
            generar_imagen_tira(
                self.formulario(), "formulario-de-prueba", posicion, configuracion
            )
            for posicion in (1, 2, 3)
        ]

        tamano_esperado = (
            api.mm_a_px(configuracion.paper_width_mm),
            api.mm_a_px(configuracion.paper_height_mm),
        )
        self.assertTrue(all(imagen.size == tamano_esperado for imagen in imagenes))
        self.assertEqual(configuracion.badge_height_mm, 80)
        self.assertAlmostEqual(
            (
                configuracion.paper_height_mm
                - configuracion.badge_height_mm * 3
            )
            / 4,
            9.85,
        )
        self.assertEqual(configuracion.global_offset_y_mm, -1)

        cajas = [self.contenido_impreso(imagen) for imagen in imagenes]
        self.assertTrue(all(caja is not None for caja in cajas))
        self.assertGreater(cajas[0][1], cajas[1][1])
        self.assertGreater(cajas[1][1], cajas[2][1])
        self.assertLessEqual(cajas[2][3], cajas[1][1])
        self.assertLessEqual(cajas[1][3], cajas[0][1])

    def test_rechaza_ajuste_que_saca_el_gafete_de_la_tira(self):
        with self.assertRaisesRegex(ValueError, "fuera de la tira"):
            generar_imagen_tira(
                self.formulario(),
                "formulario-de-prueba",
                1,
                ConfiguracionTira(),
                offset_x_mm=20,
            )

    def test_rechaza_configuracion_donde_no_caben_tres_gafetes(self):
        with self.assertRaises(ValidationError):
            ConfiguracionTira(paper_height_mm=240, badge_height_mm=85)

    def test_padding_desplaza_el_contenido_hacia_la_derecha(self):
        sin_padding = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(form_padding_left_mm=0),
        )
        con_padding = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(form_padding_left_mm=4),
        )

        caja_sin_padding = self.contenido_impreso(sin_padding)
        caja_con_padding = self.contenido_impreso(con_padding)
        self.assertGreater(caja_con_padding[0], caja_sin_padding[0])

    def test_padding_disminuye_gradualmente_de_posicion_1_a_3(self):
        configuracion = ConfiguracionTira()
        cajas = {
            posicion: self.contenido_impreso(
                generar_imagen_tira(
                    self.formulario(),
                    "formulario-de-prueba",
                    posicion,
                    configuracion,
                )
            )
            for posicion in (1, 2, 3)
        }

        self.assertGreater(cajas[1][0], cajas[2][0])
        self.assertGreater(cajas[2][0], cajas[3][0])
        self.assertEqual(
            api.padding_formulario_mm(1, configuracion.form_padding_left_mm),
            10,
        )
        self.assertEqual(
            api.padding_formulario_mm(2, configuracion.form_padding_left_mm),
            8,
        )
        self.assertEqual(
            api.padding_formulario_mm(3, configuracion.form_padding_left_mm),
            6,
        )

    @patch("app.imprimir_windows", return_value="EPSON L3310 Series")
    def test_endpoint_imprime_formulario_guardado_en_posicion(self, imprimir):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                form_id, _ = api.guardar_formulario(self.formulario())

                respuesta = api.imprimir_formulario_en_posicion(
                    form_id,
                    ImpresionPosicion(
                        position=2,
                        offset_x_mm=1,
                        printer_name="EPSON L3310 Series",
                    ),
                )

        self.assertTrue(respuesta["ok"])
        self.assertEqual(respuesta["position"], 2)
        argumentos = imprimir.call_args.args
        self.assertEqual(argumentos[1], "EPSON L3310 Series")
        self.assertEqual(argumentos[2:], (107.95, 279.4))

    @patch("app.imprimir_windows")
    def test_post_print_asigna_1_2_3_y_abre_otra_tira(self, imprimir):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                respuestas = [
                    api.imprimir(self.formulario(), simulate=True)
                    for _ in range(4)
                ]

        self.assertEqual([item["position"] for item in respuestas], [1, 2, 3, 1])
        self.assertEqual(len({item["strip_id"] for item in respuestas[:3]}), 1)
        self.assertNotEqual(respuestas[2]["strip_id"], respuestas[3]["strip_id"])
        self.assertTrue(respuestas[2]["strip_completed"])
        self.assertTrue(all(item["simulated"] for item in respuestas))
        imprimir.assert_not_called()

    @patch("app.imprimir_windows", side_effect=RuntimeError("sin papel"))
    def test_fallo_de_impresion_devuelve_la_posicion_reservada(self, imprimir):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                with self.assertRaises(HTTPException):
                    api.procesar_impresion(self.formulario(), simulate=False)

                estado_fallido = api.obtener_estado_posiciones()
                reintento = api.procesar_impresion(self.formulario(), simulate=True)

        self.assertEqual(estado_fallido["next_position"], 1)
        self.assertEqual(estado_fallido["positions"][0]["status"], "failed")
        self.assertEqual(reintento["position"], 1)

    def test_fallo_en_posicion_3_reabre_la_misma_tira(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                primera = api.procesar_impresion(self.formulario(), simulate=True)
                api.procesar_impresion(self.formulario(), simulate=True)
                with patch("app.imprimir_windows", side_effect=RuntimeError("sin papel")):
                    with self.assertRaises(HTTPException):
                        api.procesar_impresion(self.formulario(), simulate=False)

                estado = api.obtener_estado_posiciones()

        self.assertEqual(estado["strip_id"], primera["strip_id"])
        self.assertEqual(estado["next_position"], 3)

    def test_solicitudes_simultaneas_no_repiten_posicion_en_una_tira(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                with ThreadPoolExecutor(max_workers=6) as ejecutor:
                    respuestas = list(
                        ejecutor.map(
                            lambda _: api.imprimir(self.formulario(), simulate=True),
                            range(6),
                        )
                    )

        posiciones_por_tira = {}
        for respuesta in respuestas:
            posiciones_por_tira.setdefault(respuesta["strip_id"], set()).add(
                respuesta["position"]
            )
        self.assertEqual(len(posiciones_por_tira), 2)
        self.assertTrue(
            all(posiciones == {1, 2, 3} for posiciones in posiciones_por_tira.values())
        )

    def test_endpoint_permite_corregir_siguiente_posicion(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                inicial = api.obtener_estado_posiciones()
                corregido = api.configurar_estado_posiciones(
                    api.AjusteEstadoPosiciones(next_position=3)
                )
                nueva = api.configurar_estado_posiciones(
                    api.AjusteEstadoPosiciones(
                        next_position=2,
                        start_new_strip=True,
                    )
                )

        self.assertEqual(inicial["next_position"], 1)
        self.assertEqual(corregido["next_position"], 3)
        self.assertEqual(nueva["next_position"], 2)
        self.assertNotEqual(corregido["strip_id"], nueva["strip_id"])


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

    def test_guarda_y_recupera_configuracion_de_tira(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                esperada = ConfiguracionTira(global_offset_x_mm=1.5)
                api.guardar_configuracion_tira(esperada)

                recuperada = api.obtener_configuracion_tira()
                self.assertEqual(recuperada, esperada)


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
                ("POST", "/api/forms/{form_id}/print-position"),
                ("GET", "/forms/{form_id}"),
                ("GET", "/printers"),
                ("GET", "/bluetooth/ports"),
                ("GET", "/print-layout"),
                ("PUT", "/print-layout"),
                ("GET", "/print-state"),
                ("PUT", "/print-state"),
                ("POST", "/print"),
                ("POST", "/print/bluetooth"),
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

    def test_documenta_flujo_automatico_y_simulacion(self):
        esquema = api.app.openapi()

        self.assertEqual(esquema["info"]["version"], "1.1.0")
        operacion = esquema["paths"]["/print"]["post"]
        parametro_simulacion = next(
            parametro
            for parametro in operacion["parameters"]
            if parametro["name"] == "simulate"
        )
        self.assertIn("sin usar Windows", parametro_simulacion["description"])
        self.assertTrue(parametro_simulacion["schema"]["default"])

        ejemplo = esquema["components"]["schemas"][
            "ImpresionAutomaticaEnviada"
        ]["example"]
        self.assertEqual(ejemplo["position"], 1)
        self.assertEqual(ejemplo["next_position"], 2)

        ajuste = esquema["components"]["schemas"]["AjusteEstadoPosiciones"]
        self.assertIn("examples", ajuste)


if __name__ == "__main__":
    unittest.main()
