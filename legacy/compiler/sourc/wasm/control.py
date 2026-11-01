"""De labels planos a bloques anidados: la parte más delicada del backend.

El IR de Source tiene control de flujo plano, con saltos **hacia delante**:

    br_if.false cond, L0     ; ir al cuerpo del else
    <cuerpo del then>
    br L1                    ; saltar el else
  L0:
    <cuerpo del else>
  L1:

WASM no tiene labels: tiene `block` y `loop`, que se abren y cierran con `end`,
y los saltos se miden en **profundidades**, no en nombres. Peor: en WASM no se
puede saltar a un punto cualquiera del flujo, sólo al principio de un `loop` o
al final de un `block`.

La traducción sale de dos hechos sobre los rangos:

- **Un bucle** ocupa `[cabecera, extensión)`, donde la extensión es hasta donde
  acaban todos los saltos que nacen dentro (calculado por punto fijo).
- **Un destino de salto hacia delante** se representa con un `block` que
  *termina* en él, y que empieza justo antes de la construcción más interna que
  contiene a todos los saltos que lo apuntan: si empezara antes, el `end` del
  bucle interior quedaría fuera del bloque y su salida sería inalcanzable; si
  empezara después, algún salto se quedaría sin cobertura.

Con esos dos tipos de intervalo, el árbol sale de straight containment: ordenar
por inicio creciente y final decreciente, y apilar.

Y la aritmética de la profundidad se reduce a una línea:

    profundidad = nivel_del_salto - nivel_del_destino

medidas sobre la pila de construcciones abiertas. No hay casos especiales: en un
`block`, `br 0` salta a su final; en un `loop`, `br 0` salta a su principio. La
misma fórmula cubre los dos sentidos.

Nada de esto se cuenta en el emisor: aquí se construye un **plan de emisión** y
el emisor sólo lo recorre. Contar en el emisor es la fuente clásica de módulos
que validan y calculan otra cosa.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ControlFlowError(Exception):
    """El control de flujo del IR no se puede convertir a bloques.

    Es un bug del compilador, no del programa: el builder garantiza que todo
    label se declara antes de usarlo. Aquí el mensaje lleva el detalle para que
    localizarlo no cueste una hora.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message if not detail else f"{message} ({detail})")
        self.message = message
        self.detail = detail


BLOCK = "block"
LOOP = "loop"
FLAT = "flat"


@dataclass
class Jump:
    """Un salto con su profundidad ya resuelta."""

    index: int          # posición de la instrucción en el IR
    depth: int          # profundidad en construcciones WASM
    conditional: bool   # `br_if` en vez de `br`
    label: str


@dataclass
class Node:
    """Una construcción del plan.

    `BLOCK` cierra en `closes_at`; `LOOP` abre en `start` y cubre hasta `end`;
    `FLAT` sólo agrupa instrucciones para que el emisor las escriba seguidas.
    """

    kind: str
    start: int
    end: int
    label: str = ""
    children: list["Node"] = field(default_factory=list)
    closes_at: int = -1
    jumps: list[Jump] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def jump_target(instr) -> str:
    return instr.args[1] if instr.op == "br_if.false" else instr.args[0]


def instruction_labels(instrs) -> dict[str, int]:
    labels: dict[str, int] = {}
    for index, instr in enumerate(instrs):
        if instr.op != "label":
            continue
        name = instr.args[0]
        if name in labels:
            raise ControlFlowError(
                f"el label '{name}' está declarado dos veces",
                f"en {labels[name]} y en {index}")
        labels[name] = index
    return labels


def _jump_list(instrs) -> list[tuple[int, str]]:
    return [(index, jump_target(instr)) for index, instr in enumerate(instrs)
            if instr.op in ("br", "br_if.false")]


def _loop_extents(instrs, labels, loops_by_start, total) -> dict[int, int]:
    """Hasta dónde llega cada bucle.

    No es "hasta el siguiente label": dentro de un bucle puede haber un `if` con
    su propia etiqueta de salida, y esa etiqueta es del bucle, no el final de
    él. La extensión real es el punto donde acaban todos los saltos que nacen
    dentro:

        E = max(próximo label,
               destinos de saltos hacia delante dentro de [h, E),
               finales de bucles anidados dentro de [h, E))

    y se itera hasta que no cambia nada. Con cien instrucciones por función
    converge en un par de vueltas, y es mucho más barato que emitir un módulo
    mal anidado.
    """
    jumps = _jump_list(instrs)

    def following_label(position: int) -> int:
        following = [p for p in labels.values() if p > position]
        return min(following) if following else total

    cache: dict[int, int] = {}

    def extent(header: int, visiting: frozenset = frozenset()) -> int:
        if header in cache:
            return cache[header]
        if header in visiting:
            raise ControlFlowError(
                f"el bucle de la posición {header} se contiene a sí mismo")

        end = following_label(header)
        while True:
            before = end
            for index, name in jumps:
                if header <= index < end and labels[name] >= end:
                    end = labels[name]
            for nested in loops_by_start:
                if header < nested < end:
                    reach = extent(nested, visiting | {header})
                    if reach > end:
                        end = reach
            if end == before:
                break
        cache[header] = end
        return end

    return {header: extent(header) for header in loops_by_start}


def _nest(intervals, total: int) -> list[Node]:
    """Convierte intervalos en un árbol por contención.

    Ordenar por inicio creciente y final decreciente y apilar da la
    father: el intervalo más ancho contiene a los más estrechos que empiezan
    dentro.
    """
    root = Node("func", 0, total)
    stack: list[Node] = [root]
    for node in sorted(intervals, key=lambda n: (n.start, -n.end)):
        while len(stack) > 1 and stack[-1].end <= node.start:
            stack.pop()
        parent = stack[-1]
        if node.end > parent.end:
            raise ControlFlowError(
                f"la construcción en {node.start}..{node.end} se sale de su "
                f"contenedor {parent.start}..{parent.end}",
                f"{node.kind} {node.label}")
        parent.children.append(node)
        stack.append(node)
    return root.children


def _innermost_containing(nodes, position: int):
    """El nodo más interno que contiene `position`."""
    found = None
    for node in nodes:
        if node.start <= position < node.end:
            if found is None or node.start > found.start:
                found = node
    return found


def build_plan(instrs) -> tuple[Node, dict[int, Jump]]:
    """Construye el árbol de construcciones y resuelve las profundidades."""
    labels = instruction_labels(instrs)
    total = len(instrs)
    jumps_list = _jump_list(instrs)

    loop_labels: set[str] = set()
    forward_targets: dict[str, list[int]] = {}
    for index, instr in enumerate(instrs):
        if instr.op not in ("br", "br_if.false"):
            continue
        name = jump_target(instr)
        if name not in labels:
            raise ControlFlowError(
                f"salto a un label no declarado: '{name}'", f"índice {index}")
        if labels[name] <= index:
            loop_labels.add(name)
        else:
            forward_targets.setdefault(name, []).append(index)

    loops_by_start = {labels[name]: name for name in loop_labels}
    extents = _loop_extents(instrs, labels, loops_by_start, total)

    # La lista completa de bucles, no sólo los de primer nivel: para saber
    # dónde empieza un bloque hace falta saber cuál es el bucle *padre* de otro,
    # y eso sólo se ve con todos en la mano.
    loop_nodes = [Node(LOOP, header, extents[header],
                       label=loops_by_start[header])
                  for header in sorted(extents)]

    # Para cada destino de salto hacia delante, un `block` que cierra ahí. El
    # bloque empieza donde empieza la construcción que envuelve a todos sus
    # saltos: empezar antes dejaría el `end` de un bucle interior fuera de
    # alcance, y empezar después dejaría un salto sin cubrir.
    blocks: list[Node] = []
    for name, sources in sorted(forward_targets.items()):
        target = labels[name]
        # El bloque tiene que conter todos los saltos que lo apuntan. Hay dos
        # casos, y confundirlos produce un módulo que valida sin hacer lo que
        # el IR dice:
        #
        # 1. **El destino cae dentro del bucle.** El `end` del bucle está más
        #    allá y no se toca, así que el bloque se anida dentro y empieza en
        #    la cabecera del bucle.
        # 2. **El destino es la salida del bucle** (`target == bucle.end`). El
        #    bloque tiene que *contener* el bucle entero, porque es el único
        #    sitio desde el que se alcanza ese punto. Si empezara dentro,
        #    `br 0` saldría del bloque pero no del bucle, y el salto atrás
        #    del bucle acabaría con un `block` de por medio.
        node = None
        for candidate in sorted(loop_nodes, key=lambda n: -n.start):
            if all(candidate.start <= source < candidate.end
                   for source in sources):
                node = candidate
                break

        if node is None:
            start = 0
        elif target < node.end:
            start = node.start
        else:
            parent = _parent_of(loop_nodes, node)
            while parent is not None and target > parent.end:
                parent = _parent_of(loop_nodes, parent)
            start = parent.start if parent is not None else 0

        if start >= target:
            start = 0
        blocks.append(Node(BLOCK, start, target, closes_at=target))

    intervals = loop_nodes + blocks
    root = Node("func", 0, total)
    root.children = _nest(intervals, total)

    resolved = _resolve_depths(root, instrs, labels)
    return root, resolved


def _parent_of(nodes, target):
    """El bucle que contiene estrictamente a `target`, el más interno."""
    best = None
    for node in nodes:
        if node is target:
            continue
        if node.start <= target.start and node.end >= target.end:
            if best is None or node.start > best.start:
                best = node
    return best


def _own_ranges(node: Node):
    """Las instrucciones que pertenecen a `node` y no a ninguno de sus hijos.

    Los hijos son disjuntos y están ordenados, así que los huecos del rango del
    padre son los tramos que le son propios.
    """
    if node.kind == "func":
        return []
    end = node.closes_at if node.kind == BLOCK else node.end
    own: list[tuple[int, int]] = []
    cursor = node.start
    for child in node.children:
        if child.start > cursor:
            own.append((cursor, child.start))
        cursor = max(cursor, child.end)
    if cursor < end:
        own.append((cursor, end))
    return own


def _resolve_depths(root, instrs, labels) -> dict[int, Jump]:
    """Asigna a cada salto su profundidad recorriendo el plan.

    Se recorre el árbol manteniendo una pila. La profundidad es la distancia
    desde donde está el salto hasta la construcción que representa su destino:

      - destino hacia delante: el `BLOCK` que **cierra** en esa posición;
      - destino hacia atrás: el `LOOP` cuya cabecera es esa posición.
    """
    by_target: dict[int, Node] = {}
    for node in root.walk():
        if node.kind == BLOCK:
            by_target[node.closes_at] = node
        elif node.kind == LOOP:
            by_target[node.start] = node

    resolved: dict[int, Jump] = {}

    def visit(node: Node, stack: list[Node]) -> None:
        stack = stack + [node]
        # El nodo `func` no cubre ningún salto: sus hijos ya se reparten las
        # instrucciones. Si se le asignara, el salto se resolvería contra una
        # pila que no contiene el destino y siempre fallaría.
        # Una construcción asigna únicamente las instrucciones que le son
        # propias: las que caen dentro de un hijo las asigna ese hijo, con la
        # pila de uno más y por tanto la profundidad correcta. Si el padre se
        # asignara el rango entero, cada salto se resolvería dos veces y la
        # primera daría una profundidad que no corresponde.
        for start, end in _own_ranges(node):
            for index in range(start, min(end, len(instrs))):
                instr = instrs[index]
                if instr.op not in ("br", "br_if.false"):
                    continue
                name = jump_target(instr)
                destination = by_target.get(labels[name])
                if destination is None:
                    raise ControlFlowError(
                        f"ninguna construcción representa el destino '{name}'",
                        f"índice {index}")
                try:
                    from_inside = len(stack) - 1 - stack.index(destination)
                except ValueError:
                    raise ControlFlowError(
                        f"salto a '{name}' desde fuera de su bloque",
                        f"índice {index}, destino en {labels[name]}"
                    ) from None
                resolved[index] = Jump(index=index, depth=from_inside,
                                       conditional=(instr.op == "br_if.false"),
                                       label=name)
        for child in node.children:
            visit(child, stack)

    visit(root, [])
    return resolved


def describe(root: Node) -> list[str]:
    """El plan en texto. Para leerlo cuando algo no cuadra."""
    lines: list[str] = []

    def visit(node: Node, depth: int) -> None:
        pad = "  " * depth
        if node.kind == FLAT:
            lines.append(f"{pad}{node.start}..{node.end}")
            return
        if node.kind == BLOCK:
            lines.append(f"{pad}block cierra en {node.closes_at}")
        elif node.kind == LOOP:
            lines.append(f"{pad}loop {node.label} [{node.start}..{node.end})")
        for jump in node.jumps:
            lines.append(f"{pad}  br{'if' if jump.conditional else ''} "
                         f"{jump.depth} -> {jump.label}")
        for child in node.children:
            visit(child, depth + 1)

    visit(root, 0)
    return lines


def attach_jumps(root: Node, jumps: dict[int, Jump]) -> None:
    """Reparte los saltos sobre las construcciones, sólo para poder imprimirlos."""
    for node in root.walk():
        if node.kind == "func":
            continue
        ranges = ([(node.start, node.closes_at)] if node.kind == BLOCK
                  else [(node.start, node.end)])
        for start, end in ranges:
            for index in range(start, end):
                if index in jumps:
                    node.jumps.append(jumps[index])