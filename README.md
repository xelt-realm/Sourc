# Sourc

Sourc compiles `.src` programs to an AST and selects a backend by target:

- `native`: QBE IL (`.ssa`) compiled by `qbe`, then linked by `cc`.
- `web`: C emitted from the same AST and compiled to WebAssembly by Clang,
  packaged with a small Canvas 2D HTML/JavaScript host in `dist/`.

```source
var x: int = 10
if x > 5 then
  print("hola")
end
```

```sh
make
./sourc build app.src --emit-ir
./sourc build app.src --target=native
./sourc build app.src --target=web
```

The lexer reads source through `mmap`, and AST nodes use arena allocation.
`./setup_qbe.sh` installs QBE on Linux. The web target requires Clang with a
`wasm32` target and linker support. The generated WebAssembly imports Canvas
and console primitives from the local `glue.js`; it does not use a framework.

The former Python compiler, tools, and tests are retained under `legacy/`.
