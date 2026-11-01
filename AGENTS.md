# Sourc C compiler

The active compiler is C11 under `compiler/c/`.

```
.src -> mmap lexer -> arena AST -> QBE IL -> qbe | C -> clang wasm32
```

The native target and `--emit-ir` use QBE IL. The web target walks the same AST
with `codegen_c.c`, invokes Clang, and writes a self-contained Canvas 2D host
package (`dist/index.html`, `dist/glue.js`, and `dist/app.wasm`). Historical
Python code and tests are kept in `legacy/` and are not part of the active
build.

## Commands

```
make       # compile the C11 compiler from compiler/c/*.c
make test  # focused C compiler/backend tests
make clean
./setup_qbe.sh
```

Backend tools are not needed to build or test the compiler itself. Backend
integration tests mock Clang; do not invoke the wasm linker when it is
unavailable.

The lexer consumes an mmap-backed source buffer and AST nodes belong to
`Ast.arena`. Keep the frontends and backend generators deterministic and
dependency-free.
