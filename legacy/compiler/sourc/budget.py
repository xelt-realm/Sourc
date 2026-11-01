"""El gate de pesos: `budgets.toml` no es documentación, es un fallo de build.

Un presupuesto que sólo se lee no aprieta nada. Éste se comprueba: si un
programa crece por encima de su techo, `sourc size` sale con código 1 y el
build se para.

Cuatro reglas que hacen que el número signifique algo:

**El gate no depende de la máquina.** Las métricas del gate son `gzip` y `raw`,
las dos de la biblioteca estándar y las dos siempre disponibles. `brotli` es la
columna informativa, y aparece sólo si el binario está.

El motivo es concreto: con `brotli` como techo, un `make size` en una máquina
sin ese binario **fallaba**, porque "no se pudo comprimir" contaba como
incumplimiento. Un gate que necesita una herramienta que no está en el repo no
protege el repo: se desactiva el día que estorba. `gzip` es ~90% de `brotli` en
JavaScript y ~83% en HTML, así que mide casi lo mismo, y `raw` es exactamente
lo que el navegador descarga si el servidor no comprime.

**Lo que no se puede medir no se da por bueno.** Un artefacto ausente no cuenta
como cero: cuenta como *no medido*, y el informe lo dice. Un gate que suma ceros
pasa cuando en realidad no está comprobando nada, que es la forma más común de
que un presupuesto deje de servir.

**Los techos blandos y los duros se separan.** `gzip` es el objetivo, con la
tolerancia del fichero; `hard_gzip` es el muro, y pasarse no se tolera porque a
partir de ahí el número ya no describe el mismo programa.

**El pegamento es lo que descarga todo el mundo.** Por eso tiene su propio
techo blando sin margen: si una feature semántica se cuela en él, se nota. La
regla que sale de ahí es *mecanismo en el pegamento, semántica en el módulo*, y
ya se aplicó una vez --el formateador de arrays pasó del glue a
`@__printArray`, quitando 450 B de cada página.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# La calidad con la que se mide. No es la máxima del formato: 11 es lo que
# sirven los navegadores por defecto con `Content-Encoding: br`, y medir con 11
# mientras el navegador sirve con 9 daría un presupuesto que no se cumple
# nunca.
BROTLI_CALIDAD = 11

# El nivel de `gzip` que se mide. 9 es el mejor compromiso entre ratio y
# velocidad para un artefacto que se mide cientos de veces por gate; lo que
# importa es que sea **siempre el mismo**, porque el techo sólo significa algo
# comparado contra una medida hecha igual.
GZIP_NIVEL = 9


# `budgets.toml` nombra los artefactos como se despliegan; el repo los tiene en
# otra parte. Sin este mapa, el gate buscaría `source_glue.js` en la raíz y
# concluiría que el glue no existe, que es un presupuesto que no comprueba
# nada.
RUTAS_DE_ARTIFACTO = {
    "source_glue.js": "runtime/glue/glue_core.js",
    "glue_2d.js": "runtime/glue/glue_2d.js",
    "glue_3d.js": "runtime/glue/glue_3d.js",
    "glue_web.js": "runtime/glue/glue_web.js",
    "index.html": "runtime/page/index.html",
    "styles.css": "runtime/page/styles.css",
    "source_2d.wasm": "runtime/wasm/source_2d.wasm",
    "source_3d.wasm": "runtime/wasm/source_3d.wasm",
    "sourc.wasm": "build/sourc.wasm",
}


def ruta_de_artefacto(raiz: Path, nombre: str) -> Path:
    """Dónde vive en el repo un artefacto con nombre de despliegue."""
    return raiz / RUTAS_DE_ARTIFACTO.get(nombre, nombre)


@dataclass(frozen=True)
class Size:
    """El peso de un artefacto, medido de tres maneras.

    `gzip` y `raw` son las del gate y siempre existen. `brotli` es
    informativa y es `None` cuando el binario no está, que es un caso normal y
    no un fallo.
    """

    raw: int
    gzip: int
    brotli: int | None = None

    def __str__(self) -> str:
        texto = f"{self.gzip} B gzip / {self.raw} B en disco"
        if self.brotli is not None:
            texto += f" / {self.brotli} B brotli"
        return texto


@dataclass(frozen=True)
class Budget:
    """Un techo de `budgets.toml`.

    Hay dos clases y se distinguen por los campos: uno con `example` mide una
    página completa; uno con `artifact` mide un fichero.
    """

    clase: str                      # "pagina" | "artefacto"
    nombre: str                      # "main.wasm", "hola-mundo-wasm", ...
    fase: int
    artifact: str = ""
    example: str = ""
    gzip: int | None = None
    hard_gzip: int | None = None
    brotli: int | None = None          # informativa, no es techo del gate
    raw: int | None = None
    max_raw: int | None = None
    max_source_lines: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Incumplimiento:
    """Un techo que se ha pasado. `medido` es `None` cuando no se pudo medir."""

    budget: Budget
    etiqueta: str
    techo: int
    medido: int | None
    motivo: str

    def __str__(self) -> str:
        if self.medido is None:
            return (f"{self.etiqueta}: {self.motivo} (techo {self.techo} B)")
        return (f"{self.etiqueta}: {self.medido} B supera el techo de "
                f"{self.techo} B")


@dataclass
class Informe:
    """Lo que se midió y lo que no se pudo medir."""

    filas: list[tuple[str, Size, str]] = field(default_factory=list)
    incumplimientos: list[Incumplimiento] = field(default_factory=list)
    sin_medir: list[str] = field(default_factory=list)
    # Los artefactos cuyo propio techo ya se comprobó. Va en el informe y no en
    # un global del módulo: un estado global se filtra entre llamadas y el
    # segundo `sourc size` del mismo proceso se saltaría las comprobaciones.
    ya_comprobados: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.incumplimientos


# ------------------------------------------------------------------ medición


def comprimir_gzip(datos: bytes) -> int:
    """Bytes `gzip`, de la biblioteca estándar.

    Sin cabecera ni pie: son 18 bytes fijos que un servidor HTTP pone y el
    navegador nunca descarga como parte del cuerpo. Medirlos daría un techo
    inflado por una constante que no depende del artefacto.
    """
    return len(zlib.compress(datos, GZIP_NIVEL)) - 18


def comprimir_brotli(datos: bytes) -> int | None:
    """Bytes `brotli`, o `None` si no hay binario.

    Informativa, no del gate. Se lanza el proceso por artefacto porque meter una
    implementación de brotli en el compilador serían cuatrocientas líneas de
    Third Party Code para hacer lo que hace el binario del sistema, y la regla
    del repo es que una dependencia es un error de diseño.
    """
    if shutil.which("brotli") is None:
        return None
    try:
        resultado = subprocess.run(
            ["brotli", "-c", f"-q", str(BROTLI_CALIDAD)],
            input=datos, capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if resultado.returncode != 0:
        return None
    return len(resultado.stdout)


def medir(datos: bytes) -> Size:
    return Size(raw=len(datos), gzip=comprimir_gzip(datos),
                brotli=comprimir_brotli(datos))


def medir_fichero(ruta: Path) -> Size | None:
    if not ruta.is_file():
        return None
    return medir(ruta.read_bytes())


# ----------------------------------------------------------------- presupuestos


class BudgetsIlegales(ValueError):
    """`budgets.toml` que no se puede leer o que no tiene forma.

    Es una excepción propia y no el `TOMLDecodeError` de la biblioteca estándar
    porque quien lo ve es quien está ajustando un techo, y un traceback de
    `tomllib` con número de línea no le dice **qué techo** está mal ni qué
    clave se ha escrito dos veces. El error tiene que nombrar el fichero y la
    regla, como los del compilador.
    """

    def __init__(self, detalle: str, donde: str = ""):
        self.detalle = detalle
        self.donde = donde
        super().__init__(f"{detalle}{(' en ' + donde) if donde else ''}")


def cargar(ruta: Path) -> tuple[float, list[Budget]]:
    """Lee `budgets.toml` del disco. Devuelve (tolerancia, presupuestos)."""
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError as error:
        raise BudgetsIlegales(f"no se puede leer: {error}", str(ruta)) from None
    return desde_texto(texto, str(ruta))


def desde_texto(texto: str, donde: str = "budgets.toml"
                ) -> tuple[float, list[Budget]]:
    """Los presupuestos desde el texto, sin saber de dónde salió.

    Va separado de `cargar` porque el compilador distribuido como `.pyz` lee
    `budgets.toml` de dentro del archivo, donde no hay `Path` que abrir. El
    parser no necesita saber de dónde vino el texto y así no tiene que saberlo.
    """
    try:
        datos = tomllib.loads(texto)
    except tomllib.TOMLDecodeError as error:
        raise BudgetsIlegales(
            f"TOML mal formado ({error})", donde) from None
    tolerancia = float(datos.get("tolerance", 0.0))
    presupuestos: list[Budget] = []
    if "budget" not in datos:
        raise BudgetsIlegales("no hay ninguna clave `budget`", donde)
    for entrada in datos.get("budget", []):
        if not ("artifact" in entrada or "example" in entrada
                or "name" in entrada):
            raise BudgetsIlegales(
                "una entrada no dice ni `artifact`, ni `example`, ni `name`: "
                "no se sabe qué fichero mide", donde)
        es_pagina = "example" in entrada
        presupuestos.append(Budget(
            clase="pagina" if es_pagina else "artefacto",
            nombre=entrada.get("name") or entrada.get("artifact", "?"),
            fase=int(entrada.get("phase", 0)),
            artifact=entrada.get("artifact", ""),
            example=entrada.get("example", ""),
            gzip=entrada.get("gzip"),
            hard_gzip=entrada.get("hard_gzip"),
            brotli=entrada.get("brotli"),
            raw=entrada.get("raw"),
            max_raw=entrada.get("max_raw"),
            max_source_lines=entrada.get("max_source_lines"),
            note=entrada.get("note", ""),
        ))
    return tolerancia, presupuestos


# ------------------------------------------------------------------- evaluación


def _comprueba_peso(presupuesto: Budget, etiqueta: str, size: Size,
                    tolerancia: float) -> Incumplimiento | None:
    """Compara un artefacto contra un techo de peso.

    Se comprueba `gzip` y `raw`. `brotli` **no**: es informativa, y si fuera
    techo el gate volvería a depender de una herramienta del sistema.
    """
    if presupuesto.gzip is not None:
        if size.gzip > presupuesto.gzip * (1 + tolerancia):
            return Incumplimiento(presupuesto, etiqueta, presupuesto.gzip,
                                  size.gzip, "")
    if presupuesto.hard_gzip is not None and size.gzip > presupuesto.hard_gzip:
        return Incumplimiento(presupuesto, f"{etiqueta} (muro)",
                              presupuesto.hard_gzip, size.gzip, "")
    if presupuesto.raw is not None and size.raw > presupuesto.raw:
        return Incumplimiento(presupuesto, etiqueta, presupuesto.raw,
                              size.raw, "")
    if presupuesto.max_raw is not None and size.raw > presupuesto.max_raw:
        return Incumplimiento(presupuesto, etiqueta, presupuesto.max_raw,
                              size.raw, "")
    return None


def _comprueba_lineas(presupuesto: Budget, ruta: Path) -> Incumplimiento | None:
    """El techo de líneas del glue: una métrica de fuente, no de bytes."""
    if not ruta.is_file():
        return None
    lineas = len(ruta.read_text(encoding="utf-8").splitlines())
    if lineas > presupuesto.max_source_lines:
        return Incumplimiento(presupuesto, ruta.name, presupuesto.max_source_lines,
                              lineas, "")
    return None


def artefactos_estructurales(raiz: Path, presupuestos: list[Budget]) -> \
        list[tuple[Path, Budget]]:
    """Los presupuestos que se comprueban sobre ficheros del repo."""
    pares: list[tuple[Path, Budget]] = []
    for presupuesto in presupuestos:
        if presupuesto.max_source_lines is None and presupuesto.max_raw is None:
            continue
        if "*" in presupuesto.artifact:
            carpeta, _, patron = presupuesto.artifact.rpartition("/")
            base = raiz / RUTAS_DE_ARTIFACTO.get(carpeta, carpeta)
            for encontrado in sorted(base.glob(patron)):
                pares.append((encontrado, presupuesto))
        else:
            pares.append((ruta_de_artefacto(raiz, presupuesto.artifact),
                          presupuesto))
    return pares


def comprobar_programa(programa: Path, wasm: bytes, raiz: Path,
                       presupuestos: list[Budget], tolerancia: float,
                       informe: Informe,
                       runtime_modules: list[str] | None = None) -> None:
    """Mide el programa y lo compara con los presupuestos que le aplican."""
    size = medir(wasm)
    informe.filas.append((programa.name, size, "wasm"))

    # El presupuesto propio del programa gana al general. Hay programas que no
    # son comparables con `hola-mundo` --uno que existe para ejercitar veinte
    # operaciones-- y con un único techo global la única forma de cumplirlos es
    # recortar cobertura.
    propio = f"main.wasm de {programa.name}"
    main = next((b for b in presupuestos
                 if b.clase == "artefacto" and b.artifact == propio), None)
    if main is None:
        main = next((b for b in presupuestos
                     if b.clase == "artefacto" and b.artifact == "main.wasm"),
                    None)
    if main is not None:
        # La etiqueta lleva el nombre del programa: con veinte en el informe,
        # "main.wasm: 1115 B" sin decir de quién es no sirve para arreglar nada.
        fallo = _comprueba_peso(main, propio, size, tolerancia)
        if fallo:
            informe.incumplimientos.append(fallo)

    pagina = next((b for b in presupuestos
                   if b.clase == "pagina" and b.example == programa.name), None)
    if pagina is None:
        return

    # Una página es el programa más el glue y la página. Los ficheros que
    # falten se anotan como no medidos y la suma **no** se comprueba: sumar
    # ceros daría un presupuesto aprobado para una página a la que le falta la
    # mitad, que es peor que no medirla.
    piezas: list[Size] = [size]
    faltan: list[str] = []
    for nombre in _piezas_de_la_pagina(runtime_modules or []):
        ruta = ruta_de_artefacto(raiz, nombre)
        medida = medir_fichero(ruta)
        if medida is None:
            faltan.append(nombre)
            continue
        piezas.append(medida)
        informe.filas.append((ruta.name, medida, "glue"))
        propio = next((b for b in presupuestos if b.clase == "artefacto"
                       and b.artifact == nombre), None)
        # El techo propio de un artefacto es del artefacto, no del programa: con
        # veinte programas en el informe, `index.html` aparecía incumplido dos
        # veces por pasarle dos veces la misma página.
        if propio is not None and nombre not in informe.ya_comprobados:
            informe.ya_comprobados.add(nombre)
            fallo = _comprueba_peso(propio, nombre, medida, tolerancia)
            if fallo:
                informe.incumplimientos.append(fallo)

    if faltan:
        informe.sin_medir.append(
            f"página {pagina.nombre}: sin comprobar por falta de "
            f"{', '.join(sorted(faltan))}")
        return

    total = Size(raw=sum(p.raw for p in piezas),
                 gzip=sum(p.gzip for p in piezas),
                 brotli=(None if any(p.brotli is None for p in piezas)
                         else sum(p.brotli for p in piezas)))
    informe.filas.append((pagina.nombre, total, "página"))
    fallo = _comprueba_peso(pagina, pagina.nombre, total, tolerancia)
    if fallo:
        informe.incumplimientos.append(fallo)


# Lo que descarga una página, según los módulos de runtime que use el programa.
#
# El core está siempre. Los módulos no, y **esa** es la forma de que el hola
# mundo siga siendo pequeño: `glue_2d.js` son 6 KB que sólo baja quien usa
# `SOURCE_2D`. La página de un programa es, exactamente, su glue.
#
# Esta tabla estaba prometida en el docstring de la función y no existía: la
# lista era fija, así que el presupuesto de la Fase 3 para `laberinto.src` se
# estaba comprobando contra el glue del core --los números correctos para el
# programa equivocado-- y `glue_2d.js` no lo miraba nadie.
PIEZAS_POR_MODULO: dict[str, list[str]] = {
    "SOURCE_2D": ["source_2d.wasm", "glue_2d.js"],
    "SOURCE_3D": ["source_3d.wasm", "glue_3d.js"],
    "SOURCE_WEB": ["glue_web.js", "styles.css"],
}


def _piezas_de_la_pagina(runtime_modules: list[str]) -> list[str]:
    """Qué ficheros descarga una página: el core más lo que el programa usa.

    El programa compilado ya se mide aparte; aquí sólo el resto.

    El orden es estable y no depende del `USE` del fuente: los módulos se
    recorren en el orden de `PIEZAS_POR_MODULO`, que es un dict de inserción, y
    se deduplican. Un informe cuya lista de ficheros cambia con el orden de las
    líneas del fuente no sirve para comparar dos ejecuciones.
    """
    piezas = ["source_glue.js", "index.html"]
    for modulo, artefactos in PIEZAS_POR_MODULO.items():
        if modulo in runtime_modules:
            piezas += artefactos
    vistas = set()
    unicas = []
    for nombre in piezas:
        if nombre not in vistas:
            vistas.add(nombre)
            unicas.append(nombre)
    return unicas


def comprobar_estructura(raiz: Path, presupuestos: list[Budget],
                         informe: Informe) -> None:
    """Los techos que se miden sobre el repo, no sobre un programa."""
    for ruta, presupuesto in artefactos_estructurales(raiz, presupuestos):
        if not ruta.is_file():
            informe.sin_medir.append(f"{presupuesto.nombre}: no existe {ruta}")
            continue
        if presupuesto.max_source_lines is not None:
            lineas = len(ruta.read_text(encoding="utf-8").splitlines())
            informe.filas.append(
                (ruta.name, Size(raw=lineas, gzip=lineas), "líneas"))
            fallo = _comprueba_lineas(presupuesto, ruta)
            if fallo:
                informe.incumplimientos.append(fallo)
        else:
            size = medir_fichero(ruta)
            if size is None:
                continue
            informe.filas.append((ruta.name, size, "fichero"))
            fallo = _comprueba_peso(presupuesto, ruta.name, size, 0.0)
            if fallo:
                informe.incumplimientos.append(fallo)