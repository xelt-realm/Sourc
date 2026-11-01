"""Diagnósticos: errores y avisos con posición, caret y sugerencia.

El objetivo del proyecto es que un error de compilación sea más útil que la
línea de código que lo causó. Cada diagnóstico lleva:

  - código estable (`E0022`), para poder buscarlo y documentarlo;
  - span (inicio, fin), para el subrayado en el editor;
  - mensaje que dice qué hacer, no sólo qué falló;
  - sugerencia opcional, cuando existe una corrección probable.

Convenciones de códigos:
  E00xx  léxico
  E001x  errores de sentencia
  E002x  errores de expresión
  E003x  arenas
  E004x  control de flujo
  E005x  funciones
  E006x  módulos y primitivas
  W000x  avisos
"""

from __future__ import annotations

from dataclasses import dataclass, field

LINE_GUTTER = "   │ "
GUTTER_WIDTH = 3


@dataclass(frozen=True)
class Span:
    """Intervalo de media apertura, en offsets del fuente.

    `start` y `end` son índices en el texto original. `end` exclusivo.
    """

    start: int
    end: int

    def merge(self, other: "Span") -> "Span":
        lo = min(self.start, other.start)
        hi = max(self.end, other.end)
        return Span(lo, hi)

    @staticmethod
    def join(a: "Span | None", b: "Span | None") -> "Span | None":
        if a is None:
            return b
        if b is None:
            return a
        return a.merge(b)


@dataclass(frozen=True)
class Diagnostic:
    """Un error o aviso sobre el fuente."""

    code: str
    message: str
    span: Span
    suggestion: str | None = None
    is_error: bool = True

    def render(self, filename: str, source: str) -> str:
        """Renderiza el diagnóstico como un compilador real lo hace.

        Formato:
            hola.src:3:5: error[E0022]: variable no declarada 'y'
               3 │     y = 5
                 │     ^
              = nota: ¿quiso decir DIM y AS INTEGER = 5?
        """
        line_no, col_no = line_col(source, self.span.start)
        line_text = line_at(source, self.span.start)
        end_col = self.span.end - line_start_offset(source, self.span.start)

        kind = "error" if self.is_error else "aviso"
        head = f"{filename}:{line_no}:{col_no}: {kind}[{self.code}]: {self.message}"

        num = str(line_no)
        pad = " " * len(num)
        marker_len = max(1, min(end_col, len(line_text)) - col_no + 1)
        caret = " " * (col_no - 1) + "^" * marker_len

        out = [head, f"{pad} {LINE_GUTTER}{line_text}", f"{pad} {LINE_GUTTER}{caret}"]
        if self.suggestion:
            out.append(f"{pad} = nota: {self.suggestion}")
        return "\n".join(out)


def line_start_offset(source: str, offset: int) -> int:
    """Offset del inicio de la línea que contiene `offset`."""
    nl = source.rfind("\n", 0, offset)
    return 0 if nl < 0 else nl + 1


def line_col(source: str, offset: int) -> tuple[int, int]:
    """(número de línea, columna) de `offset`, ambos 1-indexados."""
    start = line_start_offset(source, offset)
    line_no = source.count("\n", 0, start) + 1
    return line_no, offset - start + 1


def line_at(source: str, offset: int) -> str:
    """Texto de la línea que contiene `offset`, sin el salto de línea."""
    start = line_start_offset(source, offset)
    end = source.find("\n", start)
    if end < 0:
        end = len(source)
    return source[start:end].rstrip("\r")


@dataclass
class DiagnosticBag:
    """Colección de diagnósticos de una compilación.

    Se ordena por posición al final para que la salida sea determinista
    independientemente del orden en que el compilador los encuentre
    (ADR 0016, requisito 3: determinismo intra-compilador).
    """

    items: list[Diagnostic] = field(default_factory=list)

    def add(self, diagnostic: Diagnostic) -> None:
        self.items.append(diagnostic)

    def error(self, code: str, message: str, span: Span, suggestion: str | None = None) -> None:
        self.items.append(Diagnostic(code, message, span, suggestion, True))

    def warn(self, code: str, message: str, span: Span, suggestion: str | None = None) -> None:
        self.items.append(Diagnostic(code, message, span, suggestion, False))

    def extend(self, other: "DiagnosticBag") -> None:
        self.items.extend(other.items)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.is_error]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if not d.is_error]

    def has_errors(self) -> bool:
        return any(d.is_error for d in self.items)

    def has_warnings(self) -> bool:
        return any(not d.is_error for d in self.items)

    def sorted(self) -> list[Diagnostic]:
        """Orden determinista: por offset, luego por código."""
        return sorted(self.items, key=lambda d: (d.span.start, d.span.end, d.code))

    def render(self, filename: str, source: str) -> str:
        return "\n".join(d.render(filename, source) for d in self.sorted())


class CompileError(Exception):
    """Error fatal de compilación: no tiene sentido seguir.

    Los errores que sí tienen sentido seguir recogiendo van en `DiagnosticBag`.
    """

    def __init__(self, diagnostic: Diagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Distancia de Levenshtein con corte temprano en `limit`.

    Se usa para sugerir correcciones. Con corte temprano devuelve `limit + 1`
    cuando la distancia real es mayor, que es todo lo que necesitamos saber.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,      # borrado
                current[j - 1] + 1,   # inserción
                previous[j - 1] + cost,  # sustitución
            )
            current.append(value)
            if value < best:
                best = value
        if best > limit:
            return limit + 1
        previous = current
    return previous[len(b)]


def suggest(name: str, candidates: list[str], limit: int | None = None) -> str | None:
    """Mejor sugerencia para un nombre mal escrito, o None.

    El límite escala con la longitud del nombre. Con un límite fijo, `y` (una
    letra) estaría a distancia 3 de `KEY` y el compilador sugeriría `KEY` al
    escribir `y = 5`, que es ruido: una sugerencia equivocada cuesta más
    confianza que una sugerencia ausente.
    """
    target = name.lower()
    if limit is None:
        limit = 1 if len(target) <= 3 else 2
    best: tuple[int, str] | None = None
    for candidate in sorted(candidates):
        distance = edit_distance(target, candidate.lower(), limit)
        if distance <= limit and (best is None or distance < best[0]):
            best = (distance, candidate)
    if best is None:
        return None
    if best[0] == 0:
        return None
    return best[1]