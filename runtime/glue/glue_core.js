// glue_core.js — el contrato entre el módulo y el mundo exterior.
//
// JavaScript a propósito: el glue es lo único que no se puede compilar a WASM,
// porque necesita el DOM, la red y el reloj. Implementa exactamente las firmas
// de HOST_SIGNATURES en compiler/sourc/wasm/backend.py; si una cambia, cambia
// este fichero, y el test `TestImportes` cruza las dos listas.

const dec = new TextDecoder();

// Modo 0, el de `PRINT` y `STR`, es la misma regla que `format_value` del
// intérprete y tiene que dar el mismo texto byte a byte. El modo 1 son
// decimales fijos, para `FORMAT(s, n)`.
export const FORMAT = { PRINT: 0, FIXED: 1 };

export function formatNumber(value, mode) {
  if (mode === FORMAT.FIXED) return value.toFixed(2);
  if (Number.isInteger(value) && Math.abs(value) < 1e15) return String(value);
  // `toPrecision(6)` redondea a seis cifras significativas; `Number` quita
  // los ceros finales. Es lo más parecido a `%.6g` que da JavaScript.
  return String(Number(value.toPrecision(6)));
}

/**
 * El corte de un frame. `flip` es la frontera entre frames y el único sitio
 * donde se sabe dónde está el bucle del programa; sin pila de llamadas, la
 * única forma de parar en el instante exacto es lanzar.
 */
export class FrameComplete extends Error {}

/**
 * El host de texto: Node, la página y los tests.
 *
 * Vive aquí porque es el mismo código en los tres sitios y tres copias del
 * mismo pegamento divergen. En la Fase 2 el dibujo se registra en vez de
 * pintarse; en la Fase 3 la página añade canvas y esto queda como la ruta de
 * texto y de tests.
 */
export function createTextHost({ frames = 1, maxFrames = null } = {}) {
  const teclas = new Set();
  const host = {
    memory: null,
    salida: "",
    display: [],
    frames: 0,
    teclas,
    out(texto) { host.salida += texto; },
    key(tecla) { return teclas.has(tecla) ? 1 : 0; },
    keydown(tecla) { return teclas.has(tecla) ? 1 : 0; },
    draw(op, args) { host.display.push(`${op} ${args.join(" ")}`); },
    flip() {
      host.frames += 1;
      host.display.push("flip");
      const tope = maxFrames === null ? frames : maxFrames;
      if (host.frames >= tope) throw new FrameComplete();
    },
  };
  return host;
}

/**
 * Los imports del módulo. La memoria se resuelve al adjuntar la instancia,
 * porque el cierre de los imports se crea antes de que exista.
 */
export function createImports(host) {
  let memory = null;
  let buffer = null;

  const view = () => {
    const m = memory || host.memory;
    if (!buffer || buffer.buffer !== m.buffer) buffer = new Uint8Array(m.buffer);
    return buffer;
  };
  const bytes = (ptr, len) => view().subarray(ptr, ptr + len);

  const write = (ptr, text) => {
    const raw = new TextEncoder().encode(text);
    view().set(raw, ptr);
    return ptr;
  };

  const imports = {
    source: {
      // ---- salida
      print_int: (v) => { host.out(String(v)); },
      print_num: (v) => { host.out(formatNumber(v, FORMAT.PRINT)); },
      print_bool: (v) => { host.out(v ? "TRUE" : "FALSE"); },
      print_str: (ptr, len) => { host.out(dec.decode(bytes(ptr, len))); },
      newline: () => { host.out("\n"); },

      // ---- formateo: escriben en la memoria del módulo y devuelven el
      // puntero. La longitud la pregunta `str_len`: dos llamadas y ningún
      // valor múltiple, que añadiría un requisito al runtime entero.
      int2str: (ptr, v) => write(ptr, String(v)),
      num2str: (ptr, v, mode) => write(ptr, formatNumber(v, mode)),
      bool2str: (ptr, v) => write(ptr, v ? "TRUE" : "FALSE"),
      str_len: (ptr) => {
        // Los textos del módulo no llevan byte nulo: la longitud se busca.
        const view8 = view();
        let len = 0;
        while (view8[ptr + len] !== 0) len++;
        return len;
      },
      str2num: (ptr, len) => {
        const text = dec.decode(bytes(ptr, len)).trim();
        const value = Number(text);
        return Number.isNaN(value) ? 0 : value;
      },

      // ---- matemática que el MVP de WASM no tiene como instrucción
      sin: (v) => Math.sin(v),
      cos: (v) => Math.cos(v),
      pow: (a, b) => Math.pow(a, b),   // el operador `^`; no hay función POW

      // ---- entrada
      key: (ptr, len) => host.key(dec.decode(bytes(ptr, len))),
      keydown: (ptr, len) => host.keydown(dec.decode(bytes(ptr, len))),

      // ---- dibujo: en la Fase 2 sólo se registra, y esa lista es lo que se
      // compara con el intérprete para demostrar que los dos dibujan igual.
      flip: () => host.flip(),
      // Un COLOR son cuatro bytes y JavaScript los ve con signo: `RGB(255,
      // 200, 0)` es 4294952960 y no -15987692. Sin `>>> 0` el color sale
      // negativo, y comparar contra el intérprete da un número distinto cada
      // vez que el byte alto tiene el bit siete a uno.
      dl_cls: (color) => host.draw("cls", [color >>> 0]),
      dl_screen: (w, h, ptr, len) =>
        host.draw("screen", [w, h, dec.decode(bytes(ptr, len))]),
      dl_screen_off: () => host.draw("screen_off", []),
      dl_rect: (x, y, w, h, color) =>
        host.draw("rect", [x, y, w, h, color >>> 0]),
      dl_circle: (x, y, r, color) => host.draw("circle", [x, y, r, color >>> 0]),
      dl_pset: (x, y, color) => host.draw("pset", [x, y, color >>> 0]),
      dl_line: (x0, y0, x1, y1, color) =>
        host.draw("line", [x0, y0, x1, y1, color >>> 0]),
      dl_triangle: (a, b, c, d, e, f, color) =>
        host.draw("triangle", [a, b, c, d, e, f, color >>> 0]),
      dl_text: (x, y, ptr, len, color) =>
        host.draw("text", [x, y, dec.decode(bytes(ptr, len)), color >>> 0]),
      dl_color: (color) => host.draw("color", [color >>> 0]),
    },
  };

  return {
    imports,
    /** El módulo exporta la memoria con este nombre; el glue la busca por él. */
    attach(instance) {
      memory = instance.exports.memory;
      buffer = null;
      host.memory = memory;
      return host;
    },
  };
}
/**
 * Carga un módulo y lo ejecuta, y para en el límite de frames.
 *
 * Vive aquí porque es la misma secuencia en Node y en el navegador, y dos
 * copias divergen: una se queda con un `attach` y la otra no, y el síntoma es
 * que en un sitio funciona y en el otro sale un texto vacío sin error.
 *
 * Devuelve el host --con la salida y la lista de display-- y el error, o `null`
 * si el módulo terminó o se paró en el límite de frames. `FrameComplete` no es
 * un fallo: es la parada.
 */
export async function ejecutar(bytes, opciones = {}) {
  const host = createTextHost(opciones);
  for (const tecla of opciones.teclas || []) host.teclas.add(tecla);
  const pegamento = createImports(host);
  const { instance } = await WebAssembly.instantiate(bytes, pegamento.imports);
  pegamento.attach(instance);
  try {
    instance.exports.main();
  } catch (fallo) {
    if (!(fallo instanceof FrameComplete)) return { host, error: fallo };
  }
  return { host, error: null };
}
