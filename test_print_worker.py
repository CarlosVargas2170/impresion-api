import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import print_worker as worker


def persona_valida(**cambios):
    persona = {
        "id": "persona-1",
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
    persona.update(cambios)
    return persona


class ColaFalsa:
    def __init__(self, personas=None):
        self.personas = list(personas or [])
        self.impresas = []
        self.fallidas = []

    def reclamar_siguiente(self):
        return self.personas.pop(0) if self.personas else None

    def marcar_impresa(self, person_id):
        self.impresas.append(person_id)

    def marcar_fallida(self, person_id, error):
        self.fallidas.append((person_id, error))


class ColaConFalloAlConfirmar(ColaFalsa):
    def marcar_impresa(self, person_id):
        raise RuntimeError("PostgreSQL no disponible")


class ConfiguracionWorkerTests(unittest.TestCase):
    def test_url_de_entorno_tiene_prioridad(self):
        with patch.dict(
            os.environ,
            {"POSTGRES_DATABASE_URL": "postgresql://entorno/db"},
            clear=False,
        ):
            self.assertEqual(
                worker.cargar_url_postgres(),
                "postgresql://entorno/db",
            )

    def test_booleano_rechaza_valor_ambiguo(self):
        with patch.dict(os.environ, {"PRINT_SIMULATE": "quizas"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "true o false"):
                worker.cargar_booleano("PRINT_SIMULATE")


class ProcesamientoWorkerTests(unittest.TestCase):
    def test_mapea_persona_al_formulario_actual(self):
        formulario = worker.formulario_desde_persona(
            persona_valida(email="Ana.Perez@EJEMPLO.COM"),
            "EPSON L3310 Series",
        )

        self.assertEqual(formulario.first_name, "ANA")
        self.assertEqual(formulario.paternal_surname, "PEREZ")
        self.assertEqual(formulario.maternal_surname, "LOPEZ")
        self.assertEqual(formulario.description, "INVITADA VIP")
        self.assertEqual(formulario.company, "NEXUS")
        self.assertEqual(formulario.job_title, "GERENTE COMERCIAL")
        self.assertEqual(formulario.email, "ana.perez@ejemplo.com")
        self.assertEqual(formulario.printer_name, "EPSON L3310 Series")

    def test_no_valida_los_datos_que_provienen_de_postgres(self):
        formulario = worker.formulario_desde_persona(
            persona_valida(
                first_name="",
                email="alvaro@example.test",
                phone_number="NO-VALIDAR",
            )
        )

        self.assertEqual(formulario.first_name, "")
        self.assertEqual(formulario.email, "alvaro@example.test")
        self.assertEqual(formulario.phone_number, "NO-VALIDAR")

    @patch("print_worker.procesar_impresion")
    def test_imprime_y_marca_registro(self, imprimir):
        imprimir.return_value = {
            "strip_id": "tira-1",
            "position": 2,
        }
        cola = ColaFalsa()

        resultado = worker.procesar_persona(
            cola,
            persona_valida(),
            simulate=False,
            printer_name=None,
        )

        self.assertTrue(resultado)
        self.assertEqual(cola.impresas, ["persona-1"])
        self.assertEqual(cola.fallidas, [])
        imprimir.assert_called_once()
        self.assertFalse(imprimir.call_args.kwargs["simulate"])

    @patch("print_worker.procesar_impresion", side_effect=RuntimeError("sin papel"))
    def test_error_de_impresion_no_detiene_la_cola(self, _imprimir):
        cola = ColaFalsa([persona_valida(), persona_valida(id="persona-2")])

        primera = worker.ejecutar_ciclo(
            cola,
            batch_size=3,
            simulate=False,
            printer_name=None,
        )
        segunda = worker.ejecutar_ciclo(
            cola,
            batch_size=3,
            simulate=False,
            printer_name=None,
        )

        self.assertEqual((primera, segunda), (1, 1))
        self.assertEqual(
            [person_id for person_id, _error in cola.fallidas],
            ["persona-1", "persona-2"],
        )
        self.assertTrue(all("sin papel" in error for _, error in cola.fallidas))

    @patch("print_worker.procesar_impresion")
    def test_fallo_al_confirmar_no_marca_como_fallida_una_hoja_impresa(self, imprimir):
        imprimir.return_value = {"strip_id": "tira-1", "position": 1}
        cola = ColaConFalloAlConfirmar()

        resultado = worker.procesar_persona(
            cola,
            persona_valida(),
            simulate=False,
            printer_name=None,
        )

        self.assertFalse(resultado)
        self.assertEqual(cola.fallidas, [])

    @patch("print_worker.procesar_impresion")
    def test_procesa_solo_un_registro_aunque_el_lote_sea_mayor(self, imprimir):
        imprimir.return_value = {"strip_id": "tira-1", "position": 1}
        cola = ColaFalsa(
            [
                persona_valida(id="persona-1"),
                persona_valida(id="persona-2"),
                persona_valida(id="persona-3"),
            ]
        )

        procesadas = worker.ejecutar_ciclo(
            cola,
            batch_size=2,
            simulate=True,
            printer_name=None,
        )

        self.assertEqual(procesadas, 1)
        self.assertEqual(cola.impresas, ["persona-1"])
        self.assertEqual(len(cola.personas), 2)


class SqlWorkerTests(unittest.TestCase):
    def test_reclamo_es_concurrente_y_atomico(self):
        consulta = " ".join(worker.CLAIM_NEXT_PERSON_SQL.split()).upper()

        self.assertIn("FOR UPDATE SKIP LOCKED", consulta)
        self.assertIn("PENDING_TO_PRINT = 0", consulta)
        self.assertIn("SET PENDING_TO_PRINT = 1", consulta)
        self.assertNotIn("PRINT_STATUS", consulta)
        self.assertNotIn("IS_ACTIVE", consulta)
        self.assertNotIn("DELETE_AFTER", consulta)
        self.assertIn("LIMIT 1", consulta)

    def test_permite_elegir_ultimo_o_primero_registrado(self):
        newest = " ".join(
            worker.CLAIM_NEXT_PERSON_SQL.format(order_direction="DESC").split()
        ).upper()
        oldest = " ".join(
            worker.CLAIM_NEXT_PERSON_SQL.format(order_direction="ASC").split()
        ).upper()

        self.assertIn("CREATED_AT DESC, ID DESC", newest)
        self.assertIn("CREATED_AT ASC, ID ASC", oldest)

    def test_rechaza_orden_desconocido(self):
        with self.assertRaisesRegex(RuntimeError, "newest u oldest"):
            worker.ColaImpresionPostgres("postgresql://test", "random")

    def test_exito_conserva_uno_y_error_vuelve_a_cero(self):
        printed = " ".join(worker.MARK_PRINTED_SQL.split()).upper()
        failed = " ".join(worker.MARK_FAILED_SQL.split()).upper()

        self.assertIn("SET PENDING_TO_PRINT = 1", printed)
        self.assertIn("SET PENDING_TO_PRINT = 0", failed)
        self.assertNotIn("PRINT_STATUS", printed)
        self.assertNotIn("PRINT_STATUS", failed)

    def test_reclamo_manual_es_atomico_y_permite_reimpresion(self):
        consulta = " ".join(worker.CLAIM_PERSON_SQL.split()).upper()

        self.assertIn("FOR UPDATE SKIP LOCKED", consulta)
        self.assertIn("ID::TEXT = %S", consulta)
        self.assertIn("PENDING_TO_PRINT = 0 OR PRINTED_AT IS NOT NULL", consulta)
        self.assertIn("SET PENDING_TO_PRINT = 1", consulta)

    def test_buscador_incluye_pendientes_e_impresas(self):
        consulta = " ".join(worker.SEARCH_PEOPLE_SQL.split()).upper()

        self.assertIn("PENDING_TO_PRINT = 0", consulta)
        self.assertIn("PRINTED_AT IS NOT NULL", consulta)
        self.assertIn("ILIKE %S", consulta)
        self.assertIn("LIMIT %S", consulta)


if __name__ == "__main__":
    unittest.main()
