#ifndef SOURC_CODEGEN_C_H
#define SOURC_CODEGEN_C_H

#include "ast.h"

#include <stddef.h>

int codegen_c(const Program *program, char **output, size_t *length);

#endif
