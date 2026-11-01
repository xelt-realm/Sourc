CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Wpedantic
SOURC := sourc
SOURCES := $(wildcard compiler/c/*.c)
HEADERS := $(wildcard compiler/c/*.h)

.DEFAULT_GOAL := all
.PHONY: all test clean deploy

all: $(SOURC)

$(SOURC): $(SOURCES) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ $(SOURCES)

test: $(SOURC)
	sh compiler/c/test_c_compiler.sh

deploy: $(SOURC)
	@echo "Limpiando la salida anterior..."
	@rm -rf dist
	@echo "Compilando Sourc..."
	@./sourc build examples/landing.src --target=web
	@echo "Verificando el paquete generado..."
	@test -d dist || { echo "Error: no se creó dist/." >&2; exit 1; }
	@test -f dist/index.html || { echo "Error: falta dist/index.html." >&2; exit 1; }
	@echo "Subiendo a Cloudflare Pages..."
	@npx wrangler pages deploy dist --project-name sourc

clean:
	rm -f $(SOURC) app app.wasm out.s out.ssa main.c
	rm -rf dist
	rm -f examples/landing.ssa examples/pong.ssa
