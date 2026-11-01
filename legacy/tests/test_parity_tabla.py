"""La tabla de paridad sólo crece, y sólo si alguien lo escribe.

`tests/parity.json` es el ratchet de ADR 0019, decisión 3. Sin él, dos cosas
que están mal a la vez se compensan y salen en verde: el intérprete cambia de
opinión **y** el módulo cambia con él. `tools/parity.py` sigue comparando los
dos, pero para eso hace falta Node; este módulo comprueba la tabla **sin Node**,
así que el lado del oráculo está vigilado también en una máquina que no lo tiene.

Tres invariantes, y cada una falla por su cuenta:

1. Todo programa de la tabla existe. Un programa borrado del corpus es un
   cambio de alcance y tiene que decirse en el fichero.
2. Todo programa del corpus está en la tabla. Un programa nuevo no entra solo:
   la cobertura crece a propósito, no por descuido.
3. La salida del intérprete sigue siendo la que dice la tabla. Un cambio de
   semántica es un cambio de fichero, y por lo tanto algo que alguien escribe a
   propósito con `make parity --update`.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, str(ROOT / "tools"))

import parity                                                    # noqa: E402


class TestTablaDeParidad(unittest.TestCase):
    def setUp(self):
        self.tabla = parity.leer_tabla()

    def test_la_tabla_existe(self):
        """Sin tabla no hay ratchet: el fichero es el mecanismo, no el test."""
        self.assertTrue(parity.TABELA.exists(),
                        f"falta {parity.TABELA.name}; se genera con "
                        "`python3 tools/parity.py --update`")

    def test_todo_programa_de_la_tabla_existe(self):
        en_disco = {p.name for p in parity.programas()}
        borrados = sorted(set(self.tabla) - en_disco)
        self.assertEqual(
            borrados, [],
            f"programas de la tabla que ya no existen: {borrados}\n"
            "Si se borraron del corpus, quita su entrada con "
            "`make parity --update`.")

    def test_todo_programa_esta_en_la_tabla(self):
        """Un programa nuevo no se registra solo: se registra a mano."""
        sin_registrar = sorted({p.name for p in parity.programas()}
                               - set(self.tabla))
        self.assertEqual(
            sin_registrar, [],
            f"programas que no están en la tabla: {sin_registrar}\n"
            "La paridad de un programa que nadie ha mirado no es paridad. "
            "Ejecuta `python3 tools/parity.py --update` y revisa el diff: "
            "un hash que cambia es un cambio de semántica o un bug.")

    def test_la_salida_del_interprete_no_ha_cambiado(self):
        """El hash es del intérprete, y el intérprete es el oráculo.

        Aquí no hace falta el módulo: si la salida del intérprete cambia, la
        tabla está desfasada y hay que decidir si el cambio es intencionado.
        """
        desfasadas = []
        for path in parity.programas():
            entrada = self.tabla.get(path.name)
            if entrada is None or "salida" not in entrada:
                continue
            actual = parity.referencia(path)
            if actual != {k: entrada[k] for k in ("salida", "display")}:
                desfasadas.append(
                    f"{path.name}: tabla dice {entrada['salida'][:8]}/"
                    f"{entrada['display'][:8]}, ahora "
                    f"{actual['salida'][:8]}/{actual['display'][:8]}")
        self.assertEqual(
            desfasadas, [],
            "cambió la salida del intérprete:\\n  " + "\\n  ".join(desfasadas)
            + "\\nSi el cambio es intencionado, `make parity --update`. Si no, "
            "es un bug del intérprete y la tabla acaba de sujetarlo.")

    def test_cada_entrada_dice_o_que_pasa_o_por_que_no(self):
        """Una entrada dice hashes o dice motivo. Nunca las dos cosas ni ninguna.

        La tabla guarda el motivo de los que fallan para que "está en la tabla"
        no signifique "está bien". Aquí no se puede comprobar que un programa
        rojo sea rojo de verdad --eso exige ejecutar el módulo, y lo hace
        `tools/parity.py`-- pero sí que la entrada tenga la forma del estado que
        dice tener.
        """
        mal_formadas = []
        for nombre, entrada in self.tabla.items():
            if "motivo" in entrada:
                continue
            if not {"salida", "display"} <= set(entrada):
                mal_formadas.append(f"{nombre}: {sorted(entrada)}")
        self.assertEqual(mal_formadas, [],
                         f"entradas sin hashes ni motivo: {mal_formadas}")


if __name__ == "__main__":
    unittest.main()
