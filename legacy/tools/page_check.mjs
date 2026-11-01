// page_check.mjs — ejecuta el script de `runtime/page/index.html` y lee su
// resultado. Sin navegador.
//
// Qué es y qué no es: el E2E de verdad habla CDP con Chrome (bloque pendiente de
// la Fase 2); esto es lo mismo sin el navegador. Pilla lo que el E2E no pilla:
// que la página referencie algo inexistente, que el import esté mal escrito,
// que el módulo no llegue a ejecutarse, que `sourcResultado` no se llene.
//
// Simula cuatro cosas, y por eso el script de la página las recibe del entorno en
// vez de depender del DOM: `document`, `location`, `window` y `fetch`. Los stubs
// son lo mínimo que deja correr el código real de la página.
//
//   node tools/page_check.mjs <programa.wasm> [frames] [nombre]
//        [--pagina <html>] [--glue <glue_core.js>]
//
// `--pagina` y `--glue` existen para poder ejecutar el `dist/` con sus
// propios ficheros. Sin ellos esta herramienta comprobaría el `runtime/` del
// repo y la afirmación de que el `dist/` "se basta solo" seguiría siendo una
// frase del manual en vez de algo que se ejecuta.

import { readFileSync, writeFileSync, mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const aqui = dirname(fileURLToPath(import.meta.url));
const raiz = join(aqui, "..");

// Los flags van aparte de los posicionales porque los posicionales ya son tres y
// un cuarto para "el pegamento" es una forma de equivocarse sin avisar. Y el flag
// se llama `--glue` y no `--pegarmento` porque en español `pegamento` y
// `pegamento` son la misma palabra escrita de dos maneras, y esa ambigüedad en
// un identificador es un `ReferenceError` esperando.
// Las rutas salen **absolutas** porque el script de la página se escribe en un
// directorio temporal: un `dist/laberinto/glue_core.js` relativo se resolvería
// contra `/tmp/...` y el import fallaría con `Cannot find package 'dist'`,
// que dice dónde se buscó y no qué se quería.
function flag(nombre, porDefecto) {
  const indice = process.argv.indexOf(nombre);
  if (indice < 0 || !process.argv[indice + 1]) {
    return porDefecto;
  }
  // Absoluto y resuelto contra el directorio de trabajo, que es donde alguien
  // escribe `dist/laberinto/...`. Resolverlo contra `tools/` --donde vive este
  // script-- daría `tools/dist/...`, que no existe.
  return resolve(process.cwd(), process.argv[indice + 1]);
}

const pagina = flag("--pagina", join(raiz, "runtime", "page", "index.html"));
const pegamento = flag("--glue", join(raiz, "runtime", "glue", "glue_core.js"));

const programa = process.argv[2];
const frames = process.argv[3] || "1";
if (!programa) {
  process.stderr.write("uso: page_check.mjs <programa.wasm> [frames]\n");
  process.exit(2);
}

const html = readFileSync(pagina, "utf8");
const encontrado = html.match(/<script type="module">([\s\S]*?)<\/script>/);
if (!encontrado) {
  process.stderr.write("la página no tiene un script de módulo\n");
  process.exit(2);
}

// El directorio donde vive el programa, con el nombre con el que la página lo
// pide. La página usa una ruta relativa porque en `dist/` está al lado. Con
// `?p=` se puede pedir otro nombre para probar el 404.
const dir = mkdtempSync(join(tmpdir(), "sourc-pagina-"));
const nombre = process.argv[4] || "main.wasm";
mkdirSync(join(dir, "descarga"));
// El programa se copia con **dos** nombres: el suyo, que es lo que se le pide
// cuando se pasa `nombre`, y `main.wasm`, que es el que la página pide por
// defecto. Con sólo uno de los dos, `--pagina dist/...` con un módulo que se
// llama `laberinto.wasm` daba un 404 que parecía un fallo del `dist/`.
for (const alias of new Set([basename(programa), "main.wasm"])) {
  writeFileSync(join(dir, "descarga", alias), readFileSync(programa));
}

// El import del pegamento se sustituye sea cual sea la ruta que lleve: en el
// repo es `../glue/glue_core.js` y en el `dist/` es `./glue_core.js`, y atarse
// a una de las dos Rompe en cuanto cambia la otra. Lo que importa es que el
// módulo que se importa sea el que se le pase.
const cuerpo = encontrado[1].replace(
  /from\s+"[^"]*glue_core\.js"/,
  `from ${JSON.stringify(pegamento)}`);
if (cuerpo === encontrado[1]) {
  process.stderr.write("la página no importa glue_core.js\n");
  process.exit(2);
}

const modulo = `
import { readFileSync } from "node:fs";
import { join } from "node:path";

const descarga = ${JSON.stringify(join(dir, "descarga"))};
const nodos = new Map();
globalThis.document = {
  getElementById(id) {
    if (!nodos.has(id)) {
      const clases = new Set();
      nodos.set(id, { textContent: "", classList: {
        toggle: (clase, activo) => (activo ? clases.add(clase)
                                            : clases.delete(clase)) } });
    }
    return nodos.get(id);
  },
};
globalThis.location = { search: "?frames=${frames}&p=${nombre}" };
globalThis.window = {};
globalThis.fetch = async (pedido) => {
  // Un 404 resuelve con ok=false, no lanza: es lo que hace un fetch de verdad, y
  // por eso la pagina tiene que mirar "ok".
  let datos;
  try {
    datos = readFileSync(join(descarga, pedido));
  } catch {
    return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
  }
  return { ok: true, status: 200, arrayBuffer: async () => datos.buffer.slice(
    datos.byteOffset, datos.byteOffset + datos.byteLength) };
};

${cuerpo}
process.stdout.write(JSON.stringify(globalThis.window.sourcResultado));
`;

const ruta = join(dir, "pagina.mjs");
writeFileSync(ruta, modulo);

const resultado = spawnSync(process.execPath, [ruta], {
  encoding: "utf8",
  timeout: 60,
});
if (resultado.status !== 0) {
  process.stderr.write(resultado.stderr.slice(0, 800));
  process.exit(resultado.status || 1);
}
process.stdout.write(resultado.stdout);
process.exit(0);
