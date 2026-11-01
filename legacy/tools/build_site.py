"""Genera el sitio: `site/pagina.src` produce el documento y aquí se convierte en un fichero.

## Por qué el CSS se mete dentro

Porque el objetivo de distribución es **un HTML que se abre con doble clic**, y
con un `styles.css` al lado eso son dos ficheros. El CSS se inlines aquí, que es
trabajo de la herramienta y no del lenguaje: el programa Source no puede emitir
una llave --`{` abre interpolación-- y por eso los estilos viven aparte. Inlinear
aquí es donde no estorba.

La contrapartida conocida: cambiar el contenido reescribe también la hoja, así
que no hay caché de estilos entre versiones. Con 3 KB de CSS es irrelevante, y
cuando la página crezca lo decide el peso y no la pereza.

## Lo que este script NO hace

**No ejecuta el programa en el servidor**, ni el navegador ni el visitante. Un
programa Source se compila a un `.wasm` y se ejecuta **en el navegador de quien
visita**. Aquí sólo se llama al compilador y se captura su salida, que es texto.
El visitante recibe HTML y un WASM, y el servidor que lo aloja no ejecuta nada
por él: es la promesa de "directorio estático" del manifiesto.

## Y lo que el `.wasm` de la página

Se publica como artefacto aparte, junto al HTML. El programa que genera el
documento también es un programa de Source, así que su módulo está disponible
para quien quiera ver **qué produce** la página, y para el E2E. No es lo que el
visitista ejecuta: lo que ejecuta es el HTML.

## Las puertas

Este script **comprueba** en vez de informar, y en este orden:

1. El documento tiene la estructura mínima y termina en `</html>`.
2. **No hay un `{` en el markup**, que no es una restricción estética: es la
   prueba de que el documento salió de un programa Source y no de un editor. Un
   literal de Source no puede contener una llave, así que si aparece, alguien la
   puso después.
3. El presupuesto de `budgets.toml` se cumple, y si no se cumple **no se
   escribe** el fichero.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
SITE = RAIZ / "site"

# El presupuesto del sitio. Va en `budgets.toml` y no aquí, porque el objetivo
# del peso es una cosa del proyecto y no de este script. Lo que este script hace
# es leerlo y no dejar pasar nada que no quepa.
CLAVE_PRESUPUESTO = "index.html de pagina.src"


class SitioInvalido(Exception):
    """El documento generado no cumple una condición. Con su motivo."""


def generar_documento(fuente: Path) -> str:
    """Ejecuta el programa Source y devuelve lo que escribe.

    Se llama al compilador como lo haría una persona, no a la biblioteca. Un
    generador que importe `interp` y ejecute en proceso es un generador que
    puede dar verde por un camino que el compilador no recorre.
    """
    resultado = subprocess.run(
        [sys.executable, "compiler/sourc.py", "run", str(fuente),
         "--salida"],
        cwd=RAIZ, capture_output=True, text=True, timeout=300)
    if resultado.returncode != 0:
        raise SitioInvalido(
            f"el programa de la página no corre:\n{resultado.stderr[-600:]}")
    return resultado.stdout


def sin_comentarios(css: str) -> str:
    """Quita los comentarios `/* ... */`.

    Los comentarios se quedan en `site/styles.css`, que es donde explican **por
    qué** una cosa es como es. Pero en el fichero que se entrega son bytes que
    paga el visitante por nada, y hay una regla escrita en `budgets.toml` para
    ese caso: un techo sin margen no admite comentarios que el usuario final
    carga.

    El regex no entiende cadenas ni `url()` dentro de comentarios anidados, y no
    hace falta: esta hoja los usa como comentarios de una línea y de bloque
    simple, y si algún día deja de ser cierto este `sin_comentarios` devuelve
    algo raro, que es visible al abrir la página.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def embeber_css(documento: str, ruta_css: Path) -> str:
    """Mete el CSS en el `</head>`, sin los comentarios.

    Se busca el `</head>` y se inserta antes. Un fallo aquí es un documento sin
    estilos, que se ve al abrirlo, y por eso se comprueba que el `</head>`
    existía en vez de asumirlo.
    """
    css = sin_comentarios(ruta_css.read_text(encoding="utf-8"))
    if "</head>" not in documento:
        raise SitioInvalido("el documento no tiene </head>, así que el CSS no "
                            "tiene dónde meterse")
    return documento.replace("</head>", f"<style>\n{css}\n</style>\n</head>", 1)


def comprobar(documento: str) -> list[str]:
    """Las condiciones del documento. Vacío es que todo va bien.

    Se leen del documento ya embutido el CSS, que es lo que se entrega: una
    comprobación sobre el documento sin CSS no dice nada del artefacto.
    """
    fallos: list[str] = []

    if not documento.lstrip().lower().startswith("<!doctype html>"):
        fallos.append("el documento no empieza por <!doctype html>")
    for necesario in ("<html", "</html>", "<head>", "<title>", "<style>",
                      "color-scheme: dark"):
        if necesario not in documento:
            fallos.append(f"falta {necesario!r}")

    # Una llave no puede venir de un literal de Source, así que si hay una es
    # que alguien la escribió después de generarlo. Es la prueba de que el
    # documento salió del programa.
    fuera_de_estilo = re.sub(r"<style>.*?</style>", "", documento, flags=re.S)
    llaves = fuera_de_estilo.count("{")
    if llaves:
        fallos.append(f"hay {llaves} llave(s) fuera del CSS: un literal de "
                      "Source no puede contenerlas, así que el documento se "
                      "tocó a mano")

    # Sin esto, un documento que no cerrara el body se vería perfecto en el
    # código y a medias en el navegador.
    if documento.count("</body>") != 1 or documento.count("<body") != 1:
        fallos.append("el body no abre y cierra exactamente una vez")
    return fallos


def medir(documento: str) -> int:
    from sourc import budget

    return budget.medir(documento.encode("utf-8")).gzip


def construir(destino: Path) -> tuple[int, int]:
    documento = embeber_css(generar_documento(SITE / "pagina.src"),
                            SITE / "styles.css")
    fallos = comprobar(documento)
    if fallos:
        raise SitioInvalido("el documento no vale:\n  - "
                            + "\n  - ".join(fallos))

    sys.path.insert(0, str(RAIZ / "compiler"))
    from sourc import budget, rutas

    tolerancia, presupuestos = budget.desde_texto(rutas.texto_de_presupuestos())
    techo = next((p for p in presupuestos
                   if p.artifact == CLAVE_PRESUPUESTO), None)
    if techo is None:
        raise SitioInvalido(f"no hay presupuesto para {CLAVE_PRESUPUESTO} en "
                            "budgets.toml. Una página sin techo es una página "
                            "que crece sin que nadie se entere.")

    tamano = len(documento.encode("utf-8"))
    gzip = medir(documento)
    if techo.gzip is not None and gzip > int(techo.gzip * (1 + tolerancia)):
        raise SitioInvalido(
            f"la página mide {gzip} B gzip y el techo es {techo.gzip} B: no "
            "se escribe el fichero")

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(documento, encoding="utf-8")
    return tamano, gzip


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="build_site",
        description="genera el sitio desde site/pagina.src")
    parser.add_argument("--salida", default="site/dist/index.html")
    args = parser.parse_args()

    try:
        tamano, gzip = construir(Path(args.salida))
    except SitioInvalido as error:
        print(error, file=sys.stderr)
        return 1

    print(f"escrito {args.salida} ({tamano} B, {gzip} B gzip)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())