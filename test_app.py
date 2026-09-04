import tempfile
import unittest
import sqlite3
import multiprocessing
from contextlib import closing
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


def intentar_propiedad_en_subproceso(
    recurso_fisico: str,
    directorio_datos: str,
    resultado,
    es_printer_id: bool = False,
) -> None:
    import app as api_hija

    api_hija.DIRECTORIO_DATOS = Path(directorio_datos)
    try:
        propiedad = (
            api_hija.poseer_printer_id(recurso_fisico)
            if es_printer_id
            else api_hija.poseer_impresora(recurso_fisico)
        )
        with propiedad:
            resultado.send("adquirido")
    except RuntimeError:
        resultado.send("bloqueado")
    finally:
        resultado.close()


class FormularioTests(unittest.TestCase):
    def datos_validos(self, **cambios):
        datos = {
            "first_name": "ANA",
            "paternal_surname": "PEREZ",
            "maternal_surname": "LOPEZ",
            "description": "INVITADA VIP",
            "company": "NEXUS",
            "job_title": "GERENTE COMERCIAL",
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
        self.assertEqual(lineas[0].strip(), "ANA")
        self.assertEqual(lineas[1].strip(), "PEREZ LOPEZ")
        self.assertEqual(lineas[2].strip(), "NEXUS")
        self.assertEqual(lineas[3].strip(), "GERENTE COMERCIAL")
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
        self.assertEqual(texto.splitlines()[1].strip(), "PEREZ")
        self.assertNotIn("NEXUS", texto)

    def test_convierte_texto_a_mayusculas_excepto_correo(self):
        formulario = Formulario(
            **self.datos_validos(
                first_name="  Ana María ",
                paternal_surname="Pérez",
                maternal_surname="López",
                description="Invitada vip",
                company="Nexus Tech",
                job_title="Gerente comercial",
                email="Ana.Perez@Ejemplo.com",
            )
        )

        self.assertEqual(formulario.first_name, "ANA MARÍA")
        self.assertEqual(formulario.paternal_surname, "PÉREZ")
        self.assertEqual(formulario.maternal_surname, "LÓPEZ")
        self.assertEqual(formulario.description, "INVITADA VIP")
        self.assertEqual(formulario.company, "NEXUS TECH")
        self.assertEqual(formulario.job_title, "GERENTE COMERCIAL")
        self.assertEqual(str(formulario.email), "ana.perez@ejemplo.com")

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
            "nombre=ANA&apellido=PEREZ+LOPEZ&telefono=%2B59171234567&"
            "email=ana.perez%40ejemplo.com&cargo=GERENTE+COMERCIAL&empresa=NEXUS"
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
            "nombre=MAR%C3%8DA+JOS%C3%89&apellido=N%C3%9A%C3%91EZ+D%27ANGELO&"
            "telefono=%2B59171234567&email=ana.perez%40ejemplo.com&"
            "cargo=I%2BD&empresa=NEXUS+%26+ASOCIADOS",
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
            "nombre=ANA&apellido=PEREZ&telefono=&email=&cargo=&empresa=",
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

    @patch("app._imprimir_windows_sin_bloqueo", return_value="Epson L3310")
    @patch("app.bloquear_impresora")
    def test_serializa_el_acceso_a_la_impresora(self, bloquear, imprimir):
        resultado = imprimir_windows(self.imagen_prueba(), "Epson L3310")

        self.assertEqual(resultado, "Epson L3310")
        bloquear.assert_called_once_with("Epson L3310")
        bloquear.return_value.__enter__.assert_called_once_with()
        bloquear.return_value.__exit__.assert_called_once()
        imprimir.assert_called_once()

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
            107,
            278,
        )

        self.assertEqual(resultado, "EPSON L3310 Series")
        dc.StartDoc.assert_called_once_with("Formulario Nexus")
        dib.return_value.draw.assert_called_once()
        destino = dib.return_value.draw.call_args.args[1]
        self.assertGreater(destino[0], 0)
        self.assertAlmostEqual(
            destino[2] - destino[0],
            api.mm_a_px(107),
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
        self.assertEqual(configuracion.paper_width_mm, 107)
        self.assertEqual(configuracion.paper_height_mm, 278)
        self.assertEqual(configuracion.badge_width_mm, 102)
        self.assertEqual(configuracion.badge_height_mm, 84)
        self.assertEqual(configuracion.outer_margin_y_mm, 13)
        self.assertEqual(configuracion.global_offset_y_mm, 0)
        self.assertEqual(
            configuracion.outer_margin_y_mm * 2
            + configuracion.badge_height_mm * 3,
            configuracion.paper_height_mm,
        )

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

    def test_rechaza_una_distribucion_con_espacios_entre_cuadros(self):
        with self.assertRaisesRegex(ValidationError, "unidos, sin espacios"):
            ConfiguracionTira(paper_height_mm=279)

    def test_padding_desplaza_el_contenido_hacia_la_derecha(self):
        sin_padding = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(
                form_padding_left_mm=0,
                content_offset_x_mm=0,
                content_offset_y_mm=0,
            ),
        )
        con_padding = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(
                form_padding_left_mm=4,
                content_offset_x_mm=0,
                content_offset_y_mm=0,
            ),
        )

        caja_sin_padding = self.contenido_impreso(sin_padding)
        caja_con_padding = self.contenido_impreso(con_padding)
        self.assertGreater(caja_con_padding[0], caja_sin_padding[0])

    def test_desplaza_el_contenido_cuatro_mm_solo_en_el_eje_longitudinal(self):
        centrada = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(content_offset_x_mm=0, content_offset_y_mm=0),
        )
        desplazada = generar_imagen_tira(
            self.formulario(),
            "formulario-de-prueba",
            2,
            ConfiguracionTira(),
        )

        caja_centrada = self.contenido_impreso(centrada)
        caja_desplazada = self.contenido_impreso(desplazada)
        diferencia = api.mm_a_px(4)
        self.assertEqual(caja_desplazada[0] - caja_centrada[0], 0)
        self.assertEqual(caja_desplazada[1] - caja_centrada[1], diferencia)

    def test_margen_y_centro_horizontal_son_iguales_en_las_tres_posiciones(self):
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

        self.assertEqual(cajas[1][0], cajas[2][0])
        self.assertEqual(cajas[2][0], cajas[3][0])
        self.assertEqual(cajas[1][2], cajas[2][2])
        self.assertEqual(cajas[2][2], cajas[3][2])
        self.assertEqual(
            cajas[1][1] - cajas[2][1],
            api.mm_a_px(configuracion.badge_height_mm),
        )
        self.assertEqual(
            cajas[2][1] - cajas[3][1],
            api.mm_a_px(configuracion.badge_height_mm),
        )
        self.assertEqual(
            api.padding_formulario_mm(1, configuracion.form_padding_left_mm),
            6,
        )
        self.assertEqual(
            api.padding_formulario_mm(2, configuracion.form_padding_left_mm),
            6,
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
                        position=1,
                        offset_x_mm=1,
                        printer_name="EPSON L3310 Series",
                    ),
                )

        self.assertTrue(respuesta["ok"])
        self.assertEqual(respuesta["position"], 1)
        argumentos = imprimir.call_args.args
        self.assertEqual(argumentos[1], "EPSON L3310 Series")
        self.assertEqual(argumentos[2:], (107, 278))

    @patch("app.imprimir_windows")
    def test_post_print_solo_asigna_posicion_configurada_y_abre_otra_tira(self, imprimir):
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

        self.assertEqual(
            [item["position"] for item in respuestas],
            [api.POSICION_IMPRESION] * 4,
        )
        self.assertEqual(len({item["strip_id"] for item in respuestas}), 4)
        self.assertTrue(all(item["strip_completed"] for item in respuestas))
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

        self.assertEqual(estado_fallido["next_position"], api.POSICION_IMPRESION)
        self.assertEqual(estado_fallido["positions"][0]["status"], "failed")
        self.assertEqual(reintento["position"], api.POSICION_IMPRESION)
        self.assertEqual(reintento["strip_id"], estado_fallido["strip_id"])

    def test_fallo_en_posicion_configurada_reabre_la_tira_fallida(self):
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

        self.assertNotEqual(estado["strip_id"], primera["strip_id"])
        self.assertEqual(estado["next_position"], api.POSICION_IMPRESION)
        self.assertEqual(
            estado["positions"][0]["position"], api.POSICION_IMPRESION
        )
        self.assertEqual(estado["positions"][0]["status"], "failed")

    def test_solicitudes_simultaneas_usan_posicion_configurada_en_tiras_separadas(self):
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
        self.assertEqual(len(posiciones_por_tira), 6)
        self.assertTrue(
            all(
                posiciones == {api.POSICION_IMPRESION}
                for posiciones in posiciones_por_tira.values()
            )
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

        self.assertEqual(inicial["next_position"], api.POSICION_IMPRESION)
        self.assertEqual(corregido["next_position"], api.POSICION_IMPRESION)
        self.assertEqual(nueva["next_position"], api.POSICION_IMPRESION)
        self.assertNotEqual(corregido["strip_id"], nueva["strip_id"])

    def test_worker_en_modo_normal_recorre_las_tres_posiciones(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                respuestas = [
                    api.procesar_impresion(self.formulario(), simulate=True)
                    for _ in range(4)
                ]

        self.assertEqual([item["position"] for item in respuestas], [1, 2, 3, 1])
        self.assertEqual(len({item["strip_id"] for item in respuestas[:3]}), 1)
        self.assertNotEqual(respuestas[2]["strip_id"], respuestas[3]["strip_id"])
        self.assertEqual(
            [item["strip_completed"] for item in respuestas],
            [False, False, True, False],
        )

    def test_pausa_conserva_posicion_y_reset_inicia_otra_tira(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                primera = api.procesar_impresion(self.formulario(), simulate=True)
                pausado = api.guardar_control_worker(
                    api.ConfiguracionControlWorker(
                        enabled=False,
                        position_mode="sequential",
                    )
                )
                reseteado = api.reiniciar_posicion_worker()

        self.assertEqual(primera["next_position"], 2)
        self.assertEqual(pausado["next_position"], 2)
        self.assertEqual(pausado["status"], "offline")
        self.assertFalse(pausado["worker_online"])
        self.assertEqual(reseteado["next_position"], 1)
        self.assertEqual(reseteado["status"], "offline")

    def test_heartbeat_confirma_que_el_proceso_worker_esta_activo(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with (
                patch.object(api, "DIRECTORIO_DATOS", directorio),
                patch.object(api, "RUTA_DB", directorio / "forms.db"),
            ):
                api.inicializar_db()
                api.registrar_heartbeat_worker(pid=4321)
                estado = api.obtener_estado_control_worker()

        self.assertTrue(estado["worker_online"])
        self.assertEqual(estado["status"], "running")
        self.assertEqual(estado["worker_pid"], 4321)
        self.assertIsNotNone(estado["last_heartbeat"])


class ImpresorasMultiplesTests(unittest.TestCase):
    def formulario(self):
        return Formulario(**FormularioTests().datos_validos())

    def test_migra_tira_legacy_activa_sin_perder_estado_ni_jobs(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            ruta_db = directorio / "forms.db"
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", ruta_db
            ):
                with closing(sqlite3.connect(ruta_db)) as conexion:
                    conexion.executescript(
                        """
                        CREATE TABLE forms (
                            id TEXT PRIMARY KEY,
                            payload TEXT NOT NULL,
                            created_at TEXT NOT NULL
                        );
                        CREATE TABLE print_strips (
                            id TEXT PRIMARY KEY,
                            next_position INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            completed_at TEXT
                        );
                        CREATE TABLE print_jobs (
                            id TEXT PRIMARY KEY,
                            strip_id TEXT NOT NULL,
                            form_id TEXT NOT NULL,
                            position INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            error TEXT
                        );
                        """
                    )
                    conexion.execute(
                        "INSERT INTO forms VALUES ('form-legacy', '{}', '2026-01-01')"
                    )
                    conexion.execute(
                        "INSERT INTO print_strips VALUES "
                        "('strip-legacy', 2, 'active', '2026-01-01', NULL)"
                    )
                    conexion.execute(
                        "INSERT INTO print_jobs VALUES "
                        "('job-legacy', 'strip-legacy', 'form-legacy', 1, "
                        "'sent', '2026-01-01', NULL)"
                    )
                    conexion.commit()

                api.inicializar_db()
                api.inicializar_db()

                with closing(sqlite3.connect(ruta_db)) as conexion:
                    tira = conexion.execute(
                        "SELECT id, printer_id, next_position, status "
                        "FROM print_strips"
                    ).fetchone()
                    trabajo = conexion.execute(
                        "SELECT id, strip_id FROM print_jobs"
                    ).fetchone()
                    indices = {
                        fila[1]
                        for fila in conexion.execute("PRAGMA index_list(print_strips)")
                    }

        self.assertEqual(tira, ("strip-legacy", "default", 2, "active"))
        self.assertEqual(trabajo, ("job-legacy", "strip-legacy"))
        self.assertIn("idx_print_strips_printer_status", indices)

    def test_sequential_y_fixed_mantienen_tiras_independientes(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", directorio / "forms.db"
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                a_primera = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_a"
                )
                b_primera = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_b"
                )
                a_segunda = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_a"
                )
                b_segunda = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_b"
                )
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(
                        position_mode="fixed", fixed_position=2
                    )
                )
                a_fija = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_a"
                )
                b_fija = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_b"
                )

        self.assertEqual(
            [a_primera["position"], b_primera["position"], a_segunda["position"], b_segunda["position"]],
            [1, 1, 2, 2],
        )
        self.assertEqual(a_primera["strip_id"], a_segunda["strip_id"])
        self.assertEqual(b_primera["strip_id"], b_segunda["strip_id"])
        self.assertNotEqual(a_primera["strip_id"], b_primera["strip_id"])
        self.assertEqual((a_fija["position"], b_fija["position"]), (2, 2))
        self.assertNotEqual(a_fija["strip_id"], b_fija["strip_id"])

    def test_fallo_y_reapertura_de_a_no_afecta_tira_activa_de_b(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", directorio / "forms.db"
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                a_uno = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_a"
                )
                api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_a"
                )
                b_uno = api.procesar_impresion(
                    self.formulario(), simulate=True, printer_id="printer_b"
                )
                with patch("app.imprimir_windows", side_effect=RuntimeError("sin papel")):
                    with self.assertRaises(HTTPException):
                        api.procesar_impresion(
                            self.formulario(),
                            simulate=False,
                            printer_id="printer_a",
                        )
                estado_a = api.obtener_estado_posiciones("printer_a")
                estado_b = api.obtener_estado_posiciones("printer_b")

        self.assertEqual(estado_a["strip_id"], a_uno["strip_id"])
        self.assertEqual(estado_a["next_position"], 3)
        self.assertEqual(estado_a["positions"][-1]["status"], "failed")
        self.assertEqual(estado_b["strip_id"], b_uno["strip_id"])
        self.assertEqual(estado_b["next_position"], 2)

    def test_reservas_concurrentes_no_mezclan_tiras(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", directorio / "forms.db"
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                impresoras = ["printer_a", "printer_b"] * 6
                with ThreadPoolExecutor(max_workers=6) as ejecutor:
                    reservas = list(
                        ejecutor.map(
                            lambda item: api.reservar_posicion(str(item[0]), item[1]),
                            enumerate(impresoras),
                        )
                    )
                with closing(sqlite3.connect(directorio / "forms.db")) as conexion:
                    tiras = conexion.execute(
                        "SELECT id, printer_id FROM print_strips"
                    ).fetchall()
                    posiciones_duplicadas = conexion.execute(
                        "SELECT strip_id, position FROM print_jobs "
                        "GROUP BY strip_id, position HAVING COUNT(*) > 1"
                    ).fetchall()

        strip_ids_por_impresora = {"printer_a": set(), "printer_b": set()}
        for reserva, printer_id in zip(reservas, impresoras, strict=True):
            strip_ids_por_impresora[printer_id].add(reserva[1])
        self.assertTrue(strip_ids_por_impresora["printer_a"])
        self.assertTrue(strip_ids_por_impresora["printer_b"])
        self.assertTrue(
            strip_ids_por_impresora["printer_a"].isdisjoint(
                strip_ids_por_impresora["printer_b"]
            )
        )
        self.assertEqual(
            {printer_id for _, printer_id in tiras}, {"default", "printer_a", "printer_b"}
        )
        self.assertEqual(posiciones_duplicadas, [])

    def test_heartbeats_y_locks_separan_recursos(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", directorio / "forms.db"
            ):
                api.inicializar_db()
                api.registrar_heartbeat_worker(pid=100, printer_id="printer_a")
                api.registrar_heartbeat_worker(pid=200, printer_id="printer_b")
                api.registrar_heartbeat_worker(pid=300)
                estado_a = api._estado_ejecucion_worker("printer_a")
                estado_b = api._estado_ejecucion_worker("printer_b")
                estado_default = api._estado_ejecucion_worker()
                with closing(sqlite3.connect(directorio / "forms.db")) as conexion:
                    claves = {
                        fila[0] for fila in conexion.execute("SELECT key FROM settings")
                    }
                ruta_a = api.ruta_bloqueo_impresora("EPSON A")
                ruta_a_misma = api.ruta_bloqueo_impresora("epson a")
                ruta_b = api.ruta_bloqueo_impresora("EPSON B")

        self.assertEqual(
            (estado_a["worker_pid"], estado_b["worker_pid"], estado_default["worker_pid"]),
            (100, 200, 300),
        )
        self.assertEqual(
            claves,
            {"worker_heartbeat", "worker_heartbeat:printer_a", "worker_heartbeat:printer_b"},
        )
        self.assertEqual(ruta_a, ruta_a_misma)
        self.assertNotEqual(ruta_a, ruta_b)
        self.assertIs(
            api._bloqueo_hilo_impresora("EPSON A"),
            api._bloqueo_hilo_impresora("epson a"),
        )
        self.assertIsNot(
            api._bloqueo_hilo_impresora("EPSON A"),
            api._bloqueo_hilo_impresora("EPSON B"),
        )

    def test_ownership_impide_dos_workers_y_no_bloquea_impresion_corta(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio):
                with api.poseer_impresora("EPSON A"):
                    contexto = multiprocessing.get_context("spawn")
                    receptor, emisor = contexto.Pipe(duplex=False)
                    proceso = contexto.Process(
                        target=intentar_propiedad_en_subproceso,
                        args=("EPSON A", str(directorio), emisor),
                    )
                    proceso.start()
                    emisor.close()
                    self.assertTrue(receptor.poll(10))
                    self.assertEqual(receptor.recv(), "bloqueado")
                    proceso.join(timeout=10)
                    self.assertFalse(proceso.is_alive())
                    self.assertEqual(proceso.exitcode, 0)
                    receptor.close()
                    with self.assertRaisesRegex(RuntimeError, "Ya existe un worker"):
                        with api.poseer_impresora("epson a"):
                            pass
                    with api.poseer_impresora("EPSON B"):
                        pass
                    with api.bloquear_impresora("EPSON A"):
                        self.assertTrue(api.ruta_bloqueo_impresora("EPSON A").exists())

                with api.poseer_impresora("EPSON A"):
                    self.assertTrue(api.ruta_propiedad_impresora("EPSON A").exists())

    def test_ownership_logico_y_fisico_son_independientes(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio):
                with api.poseer_printer_id("default"), api.poseer_impresora("EPSON A"):
                    with self.assertRaisesRegex(RuntimeError, "printer_id=default"):
                        with api.poseer_printer_id("default"):
                            pass
                    with api.poseer_printer_id("printer_b"):
                        with self.assertRaisesRegex(RuntimeError, "recurso fisico"):
                            with api.poseer_impresora("EPSON A"):
                                pass
                    with api.poseer_printer_id("printer_b"), api.poseer_impresora(
                        "EPSON B"
                    ):
                        pass

                contexto = multiprocessing.get_context("spawn")
                receptor, emisor = contexto.Pipe(duplex=False)
                with api.poseer_printer_id("default"):
                    proceso = contexto.Process(
                        target=intentar_propiedad_en_subproceso,
                        args=("default", str(directorio), emisor, True),
                    )
                    proceso.start()
                    emisor.close()
                    self.assertTrue(receptor.poll(10))
                    self.assertEqual(receptor.recv(), "bloqueado")
                    proceso.join(timeout=10)
                    self.assertFalse(proceso.is_alive())
                    self.assertEqual(proceso.exitcode, 0)
                    receptor.close()

                with api.poseer_printer_id("default"):
                    pass

    def test_normaliza_printer_id_y_libera_lock_si_falla_la_apertura(self):
        self.assertEqual(api.normalizar_printer_id(None), "default")
        self.assertEqual(api.normalizar_printer_id(""), "default")
        self.assertEqual(api.normalizar_printer_id(" Printer_B "), "printer_b")
        self.assertEqual(api.normalizar_printer_id("PRINTER_B"), "printer_b")

        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch(
                "app.ruta_propiedad_impresora"
            ) as ruta_propiedad:
                ruta_propiedad.return_value.open.side_effect = OSError("sin acceso")
                with self.assertRaisesRegex(OSError, "sin acceso"):
                    with api.poseer_impresora("EPSON A"):
                        pass
                self.assertFalse(api._bloqueo_propiedad_impresora("EPSON A").locked())

            with patch.object(api, "DIRECTORIO_DATOS", directorio):
                with api.poseer_impresora("EPSON A"):
                    pass

    def test_http_controla_tiras_por_printer_id_y_legacy_usa_default(self):
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            with patch.object(api, "DIRECTORIO_DATOS", directorio), patch.object(
                api, "RUTA_DB", directorio / "forms.db"
            ):
                api.inicializar_db()
                api.guardar_control_worker(
                    api.ConfiguracionControlWorker(position_mode="sequential")
                )
                legacy = api.imprimir(
                    Formulario(
                        **FormularioTests().datos_validos(printer_name="EPSON A")
                    ),
                    simulate=True,
                )
                printer_b = api.procesar_impresion(
                    self.formulario(),
                    simulate=True,
                    printer_id="printer_b",
                )
                with self.assertRaises(TypeError):
                    api.imprimir(
                        self.formulario(),
                        simulate=True,
                        printer_id="printer_b",
                    )
                estado_default_antes = api.consultar_estado_posiciones()
                estado_b_antes = api.consultar_estado_posiciones("printer_b")
                ajuste_b = api.configurar_estado_posiciones(
                    api.AjusteEstadoPosiciones(next_position=3),
                    printer_id="printer_b",
                )
                estado_default_despues_ajuste = api.consultar_estado_posiciones()
                reset_b = api.resetear_posicion_worker("printer_b")
                estado_b_despues_reset_b = api.consultar_estado_posiciones("printer_b")
                estado_default_despues_reset_b = api.consultar_estado_posiciones()
                reset_legacy = api.resetear_posicion_worker()
                estado_b_despues_reset_legacy = api.consultar_estado_posiciones(
                    "printer_b"
                )

        self.assertNotEqual(legacy["strip_id"], printer_b["strip_id"])
        self.assertEqual(estado_b_antes["strip_id"], printer_b["strip_id"])
        self.assertEqual(ajuste_b["strip_id"], printer_b["strip_id"])
        self.assertEqual(estado_default_antes["strip_id"], legacy["strip_id"])
        self.assertEqual(
            estado_default_despues_ajuste["strip_id"],
            legacy["strip_id"],
        )
        self.assertEqual(
            estado_default_despues_reset_b["strip_id"],
            legacy["strip_id"],
        )
        self.assertNotEqual(
            estado_b_despues_reset_b["strip_id"],
            estado_b_antes["strip_id"],
        )
        self.assertEqual(reset_b["next_position"], 1)
        self.assertEqual(
            estado_b_despues_reset_legacy["strip_id"],
            estado_b_despues_reset_b["strip_id"],
        )
        self.assertNotEqual(reset_legacy["next_position"], estado_default_antes["next_position"])


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
                self.assertEqual(formulario["first_name"], "ANA")
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


class PersonasParaImpresionTests(unittest.TestCase):
    @patch("print_worker.cargar_url_postgres", return_value="postgresql://test/db")
    @patch("print_worker.ColaImpresionPostgres")
    def test_busca_personas_por_estado_y_texto(self, cola_clase, _cargar_url):
        cola = cola_clase.return_value
        cola.buscar_personas.return_value = [
            {
                **FormularioTests().datos_validos(),
                "id": "persona-1",
                "print_state": "printed",
                "printed_at": "2026-09-03T10:00:00-04:00",
                "print_error": None,
            }
        ]
        cola.contar_personas.return_value = {
            "pending": 0,
            "processing": 0,
            "printed": 199,
        }

        resultado = api.buscar_personas_para_impresion("Ana", "printed", 10)

        self.assertEqual(resultado["count"], 1)
        self.assertEqual(resultado["counts"]["printed"], 199)
        self.assertEqual(resultado["people"][0]["id"], "persona-1")
        cola.buscar_personas.assert_called_once_with("Ana", "printed", 10, 0)
        cola.contar_personas.assert_called_once_with("Ana")

    @patch("app.procesar_impresion_manual")
    @patch("print_worker.cargar_url_postgres", return_value="postgresql://test/db")
    @patch("print_worker.ColaImpresionPostgres")
    def test_impresion_manual_reserva_e_imprime_persona(
        self,
        cola_clase,
        _cargar_url,
        procesar,
    ):
        cola = cola_clase.return_value
        cola.reclamar_persona.return_value = {
            **FormularioTests().datos_validos(),
            "id": "persona-1",
        }
        procesar.return_value = {
            "ok": True,
            "message": "Simulacion manual asignada a la posicion 3",
            "simulated": True,
            "printer": None,
            "id": "form-1",
            "strip_id": "strip-1",
            "position": 3,
            "next_position": 2,
            "strip_completed": False,
            "view_url": "http://example.test/forms/form-1",
        }

        resultado = api.imprimir_persona_manualmente(
            "persona-1",
            api.ImpresionManualPersona(simulate=True, position=3),
        )

        self.assertEqual(resultado["position"], 3)
        cola.reclamar_persona.assert_called_once_with("persona-1")
        cola.marcar_impresa.assert_called_once_with("persona-1")
        self.assertEqual(procesar.call_args.args[1], 3)
        self.assertTrue(procesar.call_args.kwargs["simulate"])


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
                ("GET", "/manual-print"),
                ("POST", "/preview"),
                ("POST", "/forms"),
                ("GET", "/api/forms/{form_id}"),
                ("GET", "/api/forms/{form_id}/qr"),
                ("GET", "/api/forms/{form_id}/print.png"),
                ("POST", "/api/forms/{form_id}/print-position"),
                ("GET", "/forms/{form_id}"),
                ("GET", "/printers"),
                ("GET", "/people"),
                ("POST", "/people/{person_id}/print"),
                ("GET", "/bluetooth/ports"),
                ("GET", "/print-layout"),
                ("PUT", "/print-layout"),
                ("GET", "/worker-control"),
                ("PUT", "/worker-control"),
                ("POST", "/worker-control/reset-position"),
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
        self.assertNotIn(
            "printer_id",
            {parametro["name"] for parametro in operacion["parameters"]},
        )

        bluetooth = esquema["paths"]["/print/bluetooth"]["post"]["parameters"]
        self.assertNotIn("printer_id", {parametro["name"] for parametro in bluetooth})

        for ruta in ("/print-state", "/worker-control/reset-position"):
            parametros = esquema["paths"][ruta][
                "post" if ruta != "/print-state" else "get"
            ]["parameters"]
            printer_id = next(
                parametro
                for parametro in parametros
                if parametro["name"] == "printer_id"
            )
            self.assertEqual(printer_id["schema"]["default"], "default")

        ejemplo = esquema["components"]["schemas"][
            "ImpresionAutomaticaEnviada"
        ]["example"]
        self.assertEqual(ejemplo["position"], 2)
        self.assertEqual(ejemplo["next_position"], 2)

        ajuste = esquema["components"]["schemas"]["AjusteEstadoPosiciones"]
        self.assertIn("examples", ajuste)


if __name__ == "__main__":
    unittest.main()
