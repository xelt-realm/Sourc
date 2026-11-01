// host_node.mjs — ejecuta un módulo en Node con el glue real.
//
// Usa el mismo host que la página (`createTextHost`): si el runner tuviera el
// suyo, cualquier diferencia entre Node y el navegador vendría de tener dos
// pegamentos distintos, y no del módulo. Eso es exactamente lo que este
// proyecto llama "un bug que sólo existe en un lado".
//
//   node tools/host_node.mjs programa.wasm [--frames N] [--keys w,a] [--display]

import { readFileSync } from "node:fs";
import { createImports, createTextHost, FrameComplete } from
  "../runtime/glue/glue_core.js";

const argv = process.argv.slice(2);
const archivo = argv.find((a) => !a.startsWith("--"));
if (!archivo) {
  process.stderr.write("uso: host_node.mjs <programa.wasm> [--frames N]\n");
  process.exit(2);
}

function opcion(nombre, porDefecto) {
  const i = argv.indexOf(`--${nombre}`);
  return i >= 0 ? argv[i + 1] : porDefecto;
}

const frames = Number(opcion("frames", "1"));
const host = createTextHost({ frames });
for (const tecla of (opcion("keys", "") || "").split(",").filter(Boolean)) {
  host.teclas.add(tecla);
}

const pegamento = createImports(host);
const bytes = readFileSync(archivo);
const { instance } = await WebAssembly.instantiate(bytes, pegamento.imports);
pegamento.attach(instance);

try {
  instance.exports.main();
} catch (error) {
  if (!(error instanceof FrameComplete)) throw error;
}

process.stdout.write(host.salida);
// La presencia de la bandera es lo que importa: `--display` no lleva valor, y
// leer el argumento siguiente devolvía `undefined`, con lo que la lista nunca
// se imprimía y la paridad comparaba contra vacío.
if (argv.includes("--display")) {
  process.stdout.write("\n-- display --\n" + host.display.join("\n") + "\n");
}
process.exit(0);