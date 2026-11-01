#include "compiler.h"

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
} Buffer;

typedef struct Variable Variable;
struct Variable {
    const char *name;
    Type type;
    size_t slot;
    size_t array_length;
    int global;
    const Stmt *arena_owner;
    Variable *next;
};

typedef struct ArenaScope ArenaScope;
struct ArenaScope {
    const Stmt *statement;
    ArenaScope *parent;
};

typedef struct {
    Type type;
    char text[64];
    size_t string_id;
} Value;

typedef struct {
    Buffer output;
    Buffer data;
    const Program *program;
    size_t temporary;
    size_t label;
    size_t string_id;
    Variable *globals;
    ArenaScope *active_arena;
    int failed;
} Generator;

typedef struct {
    Generator *generator;
    const Program *program;
    const Function *function;
    Variable *variables;
    size_t variable_count;
    int has_return;
} FunctionContext;

static int arena_contains(const ArenaScope *scope, const Stmt *owner) {
    for (; scope; scope = scope->parent) {
        if (scope->statement == owner) {
            return 1;
        }
    }
    return 0;
}

static int appendf(Buffer *buffer, const char *format, ...) {
    va_list args;
    va_start(args, format);
    va_list copy;
    va_copy(copy, args);
    int needed = vsnprintf(NULL, 0, format, copy);
    va_end(copy);
    if (needed < 0 || buffer->length > SIZE_MAX - (size_t)needed - 1) {
        va_end(args);
        return 0;
    }
    size_t required = buffer->length + (size_t)needed + 1;
    if (required > buffer->capacity) {
        size_t capacity = buffer->capacity ? buffer->capacity : 1024;
        while (capacity < required) {
            if (capacity > SIZE_MAX / 2) {
                va_end(args);
                return 0;
            }
            capacity *= 2;
        }
        char *data = realloc(buffer->data, capacity);
        if (!data) {
            va_end(args);
            return 0;
        }
        buffer->data = data;
        buffer->capacity = capacity;
    }
    vsnprintf(buffer->data + buffer->length, buffer->capacity - buffer->length,
              format, args);
    buffer->length += (size_t)needed;
    va_end(args);
    return 1;
}

static void emitf(Generator *generator, Buffer *buffer, const char *format, ...) {
    va_list args;
    va_start(args, format);
    va_list copy;
    va_copy(copy, args);
    int needed = vsnprintf(NULL, 0, format, copy);
    va_end(copy);
    if (needed < 0 || buffer->length > SIZE_MAX - (size_t)needed - 1) {
        generator->failed = 1;
        va_end(args);
        return;
    }
    size_t required = buffer->length + (size_t)needed + 1;
    if (required > buffer->capacity) {
        size_t capacity = buffer->capacity ? buffer->capacity : 1024;
        while (capacity < required) {
            if (capacity > SIZE_MAX / 2) {
                generator->failed = 1;
                va_end(args);
                return;
            }
            capacity *= 2;
        }
        char *data = realloc(buffer->data, capacity);
        if (!data) {
            generator->failed = 1;
            va_end(args);
            return;
        }
        buffer->data = data;
        buffer->capacity = capacity;
    }
    vsnprintf(buffer->data + buffer->length, buffer->capacity - buffer->length,
              format, args);
    buffer->length += (size_t)needed;
    va_end(args);
}

static void fail_at(Generator *generator, SourceLoc loc, const char *message) {
    fprintf(stderr, "%zu:%zu: %s\n", loc.line, loc.column, message);
    generator->failed = 1;
}

static const char *qbe_type(Type type) {
    switch (type) {
    case TYPE_INT:
    case TYPE_STRING:
        return "l";
    case TYPE_ARRAY_STRING:
        return NULL;
    case TYPE_BOOL:
        return "w";
    case TYPE_FLOAT:
        return "d";
    case TYPE_VOID:
        return "v";
    default:
        return NULL;
    }
}

static int is_struct_type(Type type) {
    return type >= TYPE_STRUCT_BASE;
}

static const TypeDef *find_type_by_id(const Program *program, Type type_id) {
    for (const TypeDef *type = program->types; type; type = type->next) {
        if (type->type_id == type_id) {
            return type;
        }
    }
    return NULL;
}

static const TypeField *find_type_field(const TypeDef *type,
                                        const char *name) {
    for (const TypeField *field = type ? type->fields : NULL; field;
         field = field->next) {
        if (strcmp(field->name, name) == 0) {
            return field;
        }
    }
    return NULL;
}

static size_t type_alignment(const Program *program, Type type) {
    if (type == TYPE_BOOL) {
        return 4;
    }
    if (type == TYPE_INT || type == TYPE_FLOAT || type == TYPE_STRING) {
        return 8;
    }
    if (is_struct_type(type)) {
        size_t maximum = 1;
        const TypeDef *definition = find_type_by_id(program, type);
        for (const TypeField *field = definition ? definition->fields : NULL;
             field; field = field->next) {
            size_t alignment = type_alignment(program, field->type);
            if (alignment > maximum) {
                maximum = alignment;
            }
        }
        return maximum;
    }
    return 1;
}

static size_t type_size(const Program *program, Type type) {
    if (type == TYPE_BOOL) {
        return 4;
    }
    if (type == TYPE_INT || type == TYPE_FLOAT || type == TYPE_STRING) {
        return 8;
    }
    if (is_struct_type(type)) {
        size_t offset = 0;
        size_t alignment = type_alignment(program, type);
        const TypeDef *definition = find_type_by_id(program, type);
        for (const TypeField *field = definition ? definition->fields : NULL;
             field; field = field->next) {
            size_t field_alignment =
                type_alignment(program, field->type);
            size_t field_size = type_size(program, field->type);
            offset = (offset + field_alignment - 1) &
                     ~(field_alignment - 1);
            offset += field_size;
        }
        return (offset + alignment - 1) & ~(alignment - 1);
    }
    return 0;
}

static size_t field_offset(const Program *program, Type type,
                           const TypeField *wanted) {
    size_t offset = 0;
    const TypeDef *definition = find_type_by_id(program, type);
    for (const TypeField *field = definition ? definition->fields : NULL;
         field; field = field->next) {
        size_t alignment = type_alignment(program, field->type);
        offset = (offset + alignment - 1) & ~(alignment - 1);
        if (field == wanted) {
            return offset;
        }
        offset += type_size(program, field->type);
    }
    return SIZE_MAX;
}

static int emit_type_definitions(Generator *generator) {
    for (const TypeDef *type = generator->program->types; type;
         type = type->next) {
        size_t alignment = type_alignment(generator->program, type->type_id);
        emitf(generator, &generator->data,
              "type :sourc_t_%s = align %zu { ", type->name, alignment);
        size_t index = 0;
        for (const TypeField *field = type->fields; field;
             field = field->next, index++) {
            if (index) {
                emitf(generator, &generator->data, ", ");
            }
            if (is_struct_type(field->type)) {
                const TypeDef *nested =
                    find_type_by_id(generator->program, field->type);
                if (!nested) {
                    fail_at(generator, field->loc,
                            "unknown nested struct type");
                    return 0;
                }
                emitf(generator, &generator->data, ":sourc_t_%s",
                      nested->name);
            } else {
                const char *qtype = qbe_type(field->type);
                if (!qtype) {
                    fail_at(generator, field->loc,
                            "unsupported struct field type for QBE");
                    return 0;
                }
                emitf(generator, &generator->data, "%s", qtype);
            }
        }
        emitf(generator, &generator->data, " }\n");
    }
    if (generator->program->types) {
        emitf(generator, &generator->data, "\n");
    }
    return !generator->failed;
}

static const Function *find_function(const Program *program, const char *name) {
    for (const Function *function = program->functions; function;
         function = function->next) {
        if (strcmp(function->name, name) == 0) {
            return function;
        }
    }
    return NULL;
}

static Variable *find_variable(Variable *variables, const char *name) {
    for (Variable *variable = variables; variable; variable = variable->next) {
        if (strcmp(variable->name, name) == 0) {
            return variable;
        }
    }
    return NULL;
}

static Variable *find_context_variable(FunctionContext *context,
                                      const char *name) {
    Variable *variable = find_variable(context->variables, name);
    return variable ? variable
                    : find_variable(context->generator->globals, name);
}

static int infer_expr_type(Generator *generator, const Program *program,
                           Variable *variables, const Expr *expr, Type *type) {
    if (!expr) {
        *type = TYPE_VOID;
        return 1;
    }
    switch (expr->kind) {
    case EXPR_INT:
        *type = TYPE_INT;
        return 1;
    case EXPR_FLOAT:
        *type = TYPE_FLOAT;
        return 1;
    case EXPR_BOOL:
        *type = TYPE_BOOL;
        return 1;
    case EXPR_STRING:
        *type = TYPE_STRING;
        return 1;
    case EXPR_NAME: {
        Variable *variable = find_variable(variables, expr->as.name);
        if (!variable) {
            variable = find_variable(generator->globals, expr->as.name);
        }
        if (!variable) {
            fail_at(generator, expr->loc, "unknown variable");
            return 0;
        }
        if (variable->arena_owner &&
            !arena_contains(generator->active_arena, variable->arena_owner)) {
            fail_at(generator, expr->loc,
                    "struct value cannot outlive its arena block");
            return 0;
        }
        *type = variable->type;
        return 1;
    }
    case EXPR_ARRAY:
        *type = TYPE_ARRAY_STRING;
        for (const ExprList *item = expr->as.array; item; item = item->next) {
            Type element_type;
            if (!infer_expr_type(generator, program, variables, item->expr,
                                 &element_type)) {
                return 0;
            }
            if (element_type != TYPE_STRING) {
                fail_at(generator, item->expr->loc,
                        "array[string] elements must be strings");
                return 0;
            }
        }
        return 1;
    case EXPR_INDEX: {
        Type array_type, index_type;
        if (!infer_expr_type(generator, program, variables,
                             expr->as.index.array, &array_type) ||
            !infer_expr_type(generator, program, variables,
                             expr->as.index.index, &index_type)) {
            return 0;
        }
        if (array_type != TYPE_ARRAY_STRING || index_type != TYPE_INT) {
            fail_at(generator, expr->loc,
                    "array index requires array[string] and int");
            return 0;
        }
        *type = TYPE_STRING;
        return 1;
    }
    case EXPR_FIELD: {
        Type object_type;
        if (!infer_expr_type(generator, program, variables,
                             expr->as.field.object, &object_type)) {
            return 0;
        }
        const TypeDef *definition = find_type_by_id(program, object_type);
        const TypeField *field =
            find_type_field(definition, expr->as.field.name);
        if (!field) {
            fail_at(generator, expr->loc, "unknown field for struct type");
            return 0;
        }
        *type = field->type;
        return 1;
    }
    case EXPR_UNARY:
        if (!infer_expr_type(generator, program, variables,
                             expr->as.unary.operand, type)) {
            return 0;
        }
        if (*type != TYPE_INT && *type != TYPE_FLOAT) {
            fail_at(generator, expr->loc, "unary '-' requires a number");
            return 0;
        }
        return 1;
    case EXPR_BINARY: {
        Type left, right;
        if (!infer_expr_type(generator, program, variables,
                             expr->as.binary.left, &left) ||
            !infer_expr_type(generator, program, variables,
                             expr->as.binary.right, &right)) {
            return 0;
        }
        int comparison = expr->as.binary.op >= TOK_EQUAL;
        if ((left != TYPE_INT && left != TYPE_FLOAT) || left != right) {
            fail_at(generator, expr->loc,
                    "operators require matching numeric operands");
            return 0;
        }
        *type = comparison ? TYPE_BOOL : left;
        return 1;
    }
    case EXPR_CALL: {
        const ExprList *argument = expr->as.call.arguments;
        if (strcmp(expr->as.call.name, "ui_button") == 0) {
            fail_at(generator, expr->loc,
                    "HTML UI primitives are only available for the web target");
            return 0;
        }
        if (strcmp(expr->as.call.name, "len") == 0) {
            Type argument_type;
            if (!argument || argument->next ||
                !infer_expr_type(generator, program, variables,
                                 argument->expr, &argument_type)) {
                if (!generator->failed) {
                    fail_at(generator, expr->loc, "len expects one array argument");
                }
                return 0;
            }
            if (argument_type != TYPE_ARRAY_STRING) {
                fail_at(generator, expr->loc, "len currently expects an array");
                return 0;
            }
            if (argument->expr->kind != EXPR_NAME ||
                !find_variable(generator->globals,
                               argument->expr->as.name)) {
                fail_at(generator, expr->loc,
                        "len requires a global static array name");
                return 0;
            }
            *type = TYPE_INT;
            return 1;
        }
        if (strcmp(expr->as.call.name, "str_at") == 0) {
            Type string_type, index_type;
            if (!argument || !argument->next || argument->next->next ||
                !infer_expr_type(generator, program, variables,
                                 argument->expr, &string_type) ||
                !infer_expr_type(generator, program, variables,
                                 argument->next->expr, &index_type)) {
                if (!generator->failed) {
                    fail_at(generator, expr->loc,
                            "str_at expects a string and an index");
                }
                return 0;
            }
            if (string_type != TYPE_STRING || index_type != TYPE_INT) {
                fail_at(generator, expr->loc,
                        "str_at expects a string and an int index");
                return 0;
            }
            *type = TYPE_INT;
            return 1;
        }
        const Function *function = find_function(program, expr->as.call.name);
        if (!function) {
            fail_at(generator, expr->loc, "unknown function");
            return 0;
        }
        *type = function->return_type;
        return 1;
    }
    case EXPR_KEY:
        fail_at(generator, expr->loc,
                "key input is only available for the web target");
        return 0;
    }
    return 0;
}

static int add_variable(Generator *generator, FunctionContext *context,
                        const char *name, Type type, SourceLoc loc,
                        const Stmt *arena_owner) {
    if (find_variable(context->variables, name)) {
        fail_at(generator, loc, "duplicate variable name");
        return 0;
    }
    Variable *variable = calloc(1, sizeof(*variable));
    if (!variable) {
        generator->failed = 1;
        return 0;
    }
    variable->name = name;
    variable->type = type;
    variable->slot = context->variable_count++;
    variable->arena_owner = is_struct_type(type) ? arena_owner : NULL;
    variable->next = context->variables;
    context->variables = variable;
    return 1;
}

static int gather_variables(Generator *generator, FunctionContext *context,
                            const Stmt *statements, ArenaScope *scope) {
    for (const Stmt *stmt = statements; stmt && !generator->failed;
         stmt = stmt->next) {
        ArenaScope nested_scope;
        ArenaScope *child_scope = scope;
        const Stmt *nested = NULL;
        if (stmt->kind == STMT_IF) {
            nested = stmt->as.if_stmt.body;
        } else if (stmt->kind == STMT_WHILE) {
            nested = stmt->as.while_stmt.body;
        } else if (stmt->kind == STMT_ARENA) {
            nested = stmt->as.arena_stmt.body;
            nested_scope = (ArenaScope){.statement = stmt, .parent = scope};
            child_scope = &nested_scope;
        }
        generator->active_arena = child_scope;
        if (nested &&
            !gather_variables(generator, context, nested, child_scope)) {
            return 0;
        }
        if (stmt->kind != STMT_VAR) {
            continue;
        }
        Type type = stmt->as.var.type;
        if (type == TYPE_INVALID &&
            !infer_expr_type(generator, context->program, context->variables,
                             stmt->as.var.initializer, &type)) {
            return 0;
        }
        if (type == TYPE_VOID || type == TYPE_INVALID ||
            (!qbe_type(type) && !is_struct_type(type))) {
            fail_at(generator, stmt->loc, "invalid variable type");
            return 0;
        }
        if (!add_variable(generator, context, stmt->as.var.name, type,
                          stmt->loc, scope ? scope->statement : NULL)) {
            return 0;
        }
    }
    generator->active_arena = scope;
    return !generator->failed;
}

static int gather_globals(Generator *generator, const Program *program) {
    for (const Stmt *stmt = program->globals; stmt; stmt = stmt->next) {
        Type type = stmt->as.var.type;
        if (type == TYPE_INVALID &&
            !infer_expr_type(generator, program, generator->globals,
                             stmt->as.var.initializer, &type)) {
            return 0;
        }
        if (type == TYPE_VOID || type == TYPE_INVALID ||
            (!qbe_type(type) && type != TYPE_ARRAY_STRING &&
             !is_struct_type(type))) {
            fail_at(generator, stmt->loc, "invalid global variable type");
            return 0;
        }
        size_t array_length = 0;
        if (type == TYPE_ARRAY_STRING) {
            if (!stmt->as.var.initializer ||
                stmt->as.var.initializer->kind != EXPR_ARRAY) {
                fail_at(generator, stmt->loc,
                        "global array requires an array literal initializer");
                return 0;
            }
            for (const ExprList *item =
                     stmt->as.var.initializer->as.array; item;
                 item = item->next) {
                array_length++;
            }
            if (array_length == 0) {
                fail_at(generator, stmt->loc,
                        "global array literal cannot be empty");
                return 0;
            }
        }
        if (find_variable(generator->globals, stmt->as.var.name)) {
            fail_at(generator, stmt->loc, "duplicate global variable name");
            return 0;
        }
        Variable *variable = calloc(1, sizeof(*variable));
        if (!variable) {
            generator->failed = 1;
            return 0;
        }
        variable->name = stmt->as.var.name;
        variable->type = type;
        variable->array_length = array_length;
        variable->global = 1;
        variable->next = generator->globals;
        generator->globals = variable;
    }
    return !generator->failed;
}

static int decode_string(Generator *generator, const Expr *expr, size_t id);

static int emit_globals(Generator *generator, const Program *program) {
    for (const Stmt *stmt = program->globals; stmt; stmt = stmt->next) {
        Variable *variable = find_variable(generator->globals,
                                           stmt->as.var.name);
        if (variable->type == TYPE_ARRAY_STRING) {
            size_t first_id = generator->string_id;
            size_t index = 0;
            for (const ExprList *item = stmt->as.var.initializer->as.array;
                 item; item = item->next, index++) {
                if (item->expr->kind != EXPR_STRING) {
                    fail_at(generator, item->expr->loc,
                            "global array elements must be string literals");
                    return 0;
                }
                size_t id = generator->string_id++;
                if (!decode_string(generator, item->expr, id)) {
                    return 0;
                }
            }
            emitf(generator, &generator->data,
                  "data $sourc_global_%s = align 8 { ", variable->name);
            for (index = 0; index < variable->array_length; index++) {
                size_t id = first_id + index;
                if (id == 0) {
                    emitf(generator, &generator->data, "%sl $fmt",
                          index ? ", " : "");
                } else {
                    emitf(generator, &generator->data, "%sl $str%zu",
                          index ? ", " : "", id);
                }
            }
            emitf(generator, &generator->data, " }\n");
            continue;
        }
        if (is_struct_type(variable->type)) {
            if (stmt->as.var.initializer) {
                fail_at(generator, stmt->loc,
                        "struct initializers are not supported");
                return 0;
            }
            emitf(generator, &generator->data,
                  "data $sourc_global_%s = align 8 { z %zu }\n",
                  variable->name, type_size(generator->program,
                                            variable->type));
            continue;
        }
        if (variable->type == TYPE_FLOAT) {
            double initial_value = 0.0;
            if (stmt->as.var.initializer) {
                if (stmt->as.var.initializer->kind != EXPR_FLOAT) {
                    fail_at(generator, stmt->loc,
                            "QBE float global requires a float literal initializer");
                    return 0;
                }
                initial_value = stmt->as.var.initializer->as.floating;
            }
            uint64_t bits;
            memcpy(&bits, &initial_value, sizeof(bits));
            emitf(generator, &generator->data,
                  "data $sourc_global_%s = align 8 { l %llu }\n",
                  variable->name, (unsigned long long)bits);
            continue;
        }
        long long value = 0;
        if (stmt->as.var.initializer) {
            const Expr *initializer = stmt->as.var.initializer;
            Type initializer_type;
            if (!infer_expr_type(generator, program, generator->globals,
                                 initializer, &initializer_type) ||
                initializer_type != variable->type) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "initializer type does not match global variable type");
                }
                return 0;
            }
            if (variable->type == TYPE_INT && initializer->kind == EXPR_INT) {
                value = initializer->as.integer;
            } else if (variable->type == TYPE_BOOL &&
                       initializer->kind == EXPR_BOOL) {
                value = initializer->as.boolean;
            } else {
                fail_at(generator, stmt->loc,
                        "QBE global initializer must be an integer or boolean literal");
                return 0;
            }
        }
        emitf(generator, &generator->data,
              "data $sourc_global_%s = align %d { %s %lld }\n",
              variable->name, variable->type == TYPE_BOOL ? 4 : 8,
              qbe_type(variable->type), value);
    }
    if (program->globals) {
        emitf(generator, &generator->data, "\n");
    }
    return !generator->failed;
}

static const char *new_temp(Generator *generator, char *buffer, size_t size) {
    snprintf(buffer, size, "%%v%zu", generator->temporary++);
    return buffer;
}

static int emit_struct_address(FunctionContext *context, const Expr *expr,
                               char *result, size_t result_size) {
    Generator *generator = context->generator;
    if (expr->kind == EXPR_NAME) {
        Variable *variable =
            find_context_variable(context, expr->as.name);
        if (!variable || !is_struct_type(variable->type)) {
            fail_at(generator, expr->loc,
                    "field access requires a struct value");
            return 0;
        }
        if (variable->global) {
            snprintf(result, result_size, "$sourc_global_%s", variable->name);
        } else {
            snprintf(result, result_size, "%%slot%zu", variable->slot);
        }
        return 1;
    }
    if (expr->kind == EXPR_FIELD) {
        Type object_type;
        if (!infer_expr_type(generator, context->program, context->variables,
                             expr->as.field.object, &object_type)) {
            return 0;
        }
        const TypeDef *definition =
            find_type_by_id(context->program, object_type);
        const TypeField *field =
            find_type_field(definition, expr->as.field.name);
        size_t offset = field ? field_offset(context->program, object_type,
                                             field) : SIZE_MAX;
        if (!field || offset == SIZE_MAX || !is_struct_type(field->type)) {
            fail_at(generator, expr->loc,
                    "nested field access requires a struct field");
            return 0;
        }
        char base[64];
        if (!emit_struct_address(context, expr->as.field.object, base,
                                 sizeof(base))) {
            return 0;
        }
        if (offset == 0) {
            snprintf(result, result_size, "%s", base);
        } else {
            char temporary[64];
            const char *name = new_temp(generator, temporary,
                                        sizeof(temporary));
            emitf(generator, &generator->output,
                  "    %s =l add %s, %zu\n", name, base, offset);
            snprintf(result, result_size, "%s", name);
        }
        return 1;
    }
    fail_at(generator, expr->loc, "field access requires a struct value");
    return 0;
}

static int emit_field_address(FunctionContext *context, const Expr *expr,
                              Type *field_type, char *result,
                              size_t result_size) {
    Generator *generator = context->generator;
    Type object_type;
    if (!infer_expr_type(generator, context->program, context->variables,
                         expr->as.field.object, &object_type)) {
        return 0;
    }
    const TypeDef *definition =
        find_type_by_id(context->program, object_type);
    const TypeField *field =
        find_type_field(definition, expr->as.field.name);
    size_t offset = field ? field_offset(context->program, object_type, field)
                          : SIZE_MAX;
    if (!field || offset == SIZE_MAX) {
        fail_at(generator, expr->loc, "unknown field for struct type");
        return 0;
    }
    char base[64];
    if (!emit_struct_address(context, expr->as.field.object, base,
                             sizeof(base))) {
        return 0;
    }
    if (offset == 0) {
        snprintf(result, result_size, "%s", base);
    } else {
        char temporary[64];
        const char *name = new_temp(generator, temporary, sizeof(temporary));
        emitf(generator, &generator->output,
              "    %s =l add %s, %zu\n", name, base, offset);
        snprintf(result, result_size, "%s", name);
    }
    *field_type = field->type;
    return 1;
}

static void emit_zero_struct_slot(FunctionContext *context,
                                  const Variable *variable) {
    Generator *generator = context->generator;
    size_t size = type_size(context->program, variable->type);
    for (size_t offset = 0; offset < size; offset += 8) {
        if (offset == 0) {
            emitf(generator, &generator->output,
                  "    storel 0, %%slot%zu\n", variable->slot);
        } else {
            char address_temp[64];
            const char *address = new_temp(generator, address_temp,
                                           sizeof(address_temp));
            emitf(generator, &generator->output,
                  "    %s =l add %%slot%zu, %zu\n"
                  "    storel 0, %s\n",
                  address, variable->slot, offset, address);
        }
    }
}

static int decode_string(Generator *generator, const Expr *expr, size_t id) {
    if (id == 0) {
        emitf(generator, &generator->data, "data $fmt = { b \"");
    } else {
        emitf(generator, &generator->data, "data $str%zu = { b \"", id);
    }
    for (size_t i = 0; i < expr->as.string.length; i++) {
        unsigned char byte = (unsigned char)expr->as.string.data[i];
        if (byte == '\\') {
            if (++i >= expr->as.string.length) {
                fail_at(generator, expr->loc, "incomplete string escape");
                return 0;
            }
            switch (expr->as.string.data[i]) {
            case 'n': byte = '\n'; break;
            case 'r': byte = '\r'; break;
            case 't': byte = '\t'; break;
            case '0': byte = '\0'; break;
            case '\\': byte = '\\'; break;
            case '"': byte = '"'; break;
            default:
                fail_at(generator, expr->loc, "unsupported string escape");
                return 0;
            }
        }
        if (byte == '%') {
            emitf(generator, &generator->data, "%%%%");
        } else if (byte == '\\') {
            emitf(generator, &generator->data, "\\\\");
        } else if (byte == '"') {
            emitf(generator, &generator->data, "\\\"");
        } else if (byte == '\n') {
            emitf(generator, &generator->data, "\\n");
        } else if (byte == '\r') {
            emitf(generator, &generator->data, "\\r");
        } else if (byte == '\t') {
            emitf(generator, &generator->data, "\\t");
        } else if (byte == '\0') {
            emitf(generator, &generator->data, "\\0");
        } else if (byte < 32 || byte > 126) {
            emitf(generator, &generator->data, "\\x%02x", byte);
        } else {
            emitf(generator, &generator->data, "%c", byte);
        }
    }
    emitf(generator, &generator->data, "\\0\" }\n");
    return !generator->failed;
}

static Value emit_expr(FunctionContext *context, const Expr *expr);

static Value emit_expr(FunctionContext *context, const Expr *expr) {
    Generator *generator = context->generator;
    Value value = {.type = TYPE_INVALID, .text = {0}};
    if (!expr || generator->failed) {
        return value;
    }
    switch (expr->kind) {
    case EXPR_INT:
        value.type = TYPE_INT;
        snprintf(value.text, sizeof(value.text), "%lld", expr->as.integer);
        return value;
    case EXPR_FLOAT:
        value.type = TYPE_FLOAT;
        snprintf(value.text, sizeof(value.text), "d_%a", expr->as.floating);
        return value;
    case EXPR_BOOL:
        value.type = TYPE_BOOL;
        snprintf(value.text, sizeof(value.text), "%d", expr->as.boolean);
        return value;
    case EXPR_STRING:
        value.type = TYPE_STRING;
        value.string_id = generator->string_id++;
        if (!decode_string(generator, expr, value.string_id)) {
            generator->failed = 1;
        }
        if (value.string_id == 0) {
            snprintf(value.text, sizeof(value.text), "$fmt");
        } else {
            snprintf(value.text, sizeof(value.text), "$str%zu", value.string_id);
        }
        return value;
    case EXPR_NAME: {
        Variable *variable =
            find_context_variable(context, expr->as.name);
        if (!variable) {
            fail_at(generator, expr->loc, "unknown variable");
            return value;
        }
        if (variable->type == TYPE_ARRAY_STRING) {
            fail_at(generator, expr->loc,
                    "array values are only valid as len or index operands");
            return value;
        }
        if (is_struct_type(variable->type)) {
            fail_at(generator, expr->loc,
                    "struct values are only valid for field access");
            return value;
        }
        char temp[64];
        const char *name = new_temp(generator, temp, sizeof(temp));
        if (variable->global) {
            emitf(generator, &generator->output,
                  "    %s =%s load%s $sourc_global_%s\n",
                  name, qbe_type(variable->type), qbe_type(variable->type),
                  variable->name);
        } else {
            emitf(generator, &generator->output,
                  "    %s =%s load%s %%slot%zu\n",
                  name, qbe_type(variable->type), qbe_type(variable->type),
                  variable->slot);
        }
        value.type = variable->type;
        snprintf(value.text, sizeof(value.text), "%s", name);
        return value;
    }
    case EXPR_ARRAY:
        fail_at(generator, expr->loc,
                "array literals are only valid as global initializers");
        return value;
    case EXPR_INDEX: {
        const Expr *array_expr = expr->as.index.array;
        if (array_expr->kind != EXPR_NAME) {
            fail_at(generator, expr->loc,
                    "array indexing requires a global static array name");
            return value;
        }
        Variable *array = find_variable(generator->globals,
                                        array_expr->as.name);
        Value index = emit_expr(context, expr->as.index.index);
        if (!array || array->type != TYPE_ARRAY_STRING ||
            index.type != TYPE_INT) {
            fail_at(generator, expr->loc,
                    "array index requires array[string] and int");
            return value;
        }
        char offset_temp[64], address_temp[64], value_temp[64];
        const char *offset = new_temp(generator, offset_temp,
                                      sizeof(offset_temp));
        const char *address = new_temp(generator, address_temp,
                                       sizeof(address_temp));
        const char *loaded = new_temp(generator, value_temp,
                                      sizeof(value_temp));
        emitf(generator, &generator->output,
              "    %s =l mul %s, 8\n"
              "    %s =l add $sourc_global_%s, %s\n"
              "    %s =l loadl %s\n",
              offset, index.text, address, array->name, offset, loaded, address);
        value.type = TYPE_STRING;
        snprintf(value.text, sizeof(value.text), "%s", loaded);
        return value;
    }
    case EXPR_FIELD: {
        Type field_type;
        char address[64];
        if (!emit_field_address(context, expr, &field_type, address,
                                sizeof(address))) {
            return value;
        }
        const char *qtype = qbe_type(field_type);
        if (!qtype) {
            fail_at(generator, expr->loc,
                    "this struct field type is not supported by QBE");
            return value;
        }
        char temporary[64];
        const char *name = new_temp(generator, temporary, sizeof(temporary));
        emitf(generator, &generator->output, "    %s =%s load%s %s\n",
              name, qtype, qtype, address);
        value.type = field_type;
        snprintf(value.text, sizeof(value.text), "%s", name);
        return value;
    }
    case EXPR_UNARY: {
        Value operand = emit_expr(context, expr->as.unary.operand);
        const char *qtype = qbe_type(operand.type);
        if (operand.type != TYPE_INT && operand.type != TYPE_FLOAT) {
            fail_at(generator, expr->loc, "unary '-' requires a number");
            return value;
        }
        char temp[64];
        const char *name = new_temp(generator, temp, sizeof(temp));
        if (operand.type == TYPE_FLOAT) {
            emitf(generator, &generator->output,
                  "    %s =%s neg %s\n", name, qtype, operand.text);
        } else {
            emitf(generator, &generator->output,
                  "    %s =%s sub 0, %s\n", name, qtype, operand.text);
        }
        value.type = operand.type;
        snprintf(value.text, sizeof(value.text), "%s", name);
        return value;
    }
    case EXPR_BINARY: {
        Value left = emit_expr(context, expr->as.binary.left);
        Value right = emit_expr(context, expr->as.binary.right);
        TokenKind op = (TokenKind)expr->as.binary.op;
        const char *instruction = NULL;
        Type result_type = TYPE_INT;
        if ((left.type != TYPE_INT && left.type != TYPE_FLOAT) ||
            left.type != right.type) {
            fail_at(generator, expr->loc,
                    "operators require matching numeric operands");
            return value;
        }
        int floating = left.type == TYPE_FLOAT;
        switch (op) {
        case TOK_PLUS: instruction = "add"; break;
        case TOK_MINUS: instruction = "sub"; break;
        case TOK_STAR: instruction = "mul"; break;
        case TOK_SLASH: instruction = "div"; break;
        case TOK_EQUAL:
            instruction = floating ? "ceqd" : "ceql";
            result_type = TYPE_BOOL;
            break;
        case TOK_NOT_EQUAL:
            instruction = floating ? "cned" : "cnel";
            result_type = TYPE_BOOL;
            break;
        case TOK_LESS:
            instruction = floating ? "cltd" : "csltl";
            result_type = TYPE_BOOL;
            break;
        case TOK_LESS_EQUAL:
            instruction = floating ? "cled" : "cslel";
            result_type = TYPE_BOOL;
            break;
        case TOK_GREATER:
            instruction = floating ? "cgtd" : "csgtl";
            result_type = TYPE_BOOL;
            break;
        case TOK_GREATER_EQUAL:
            instruction = floating ? "cged" : "csgel";
            result_type = TYPE_BOOL;
            break;
        default:
            fail_at(generator, expr->loc, "unsupported binary operator");
            return value;
        }
        char temp[64];
        const char *name = new_temp(generator, temp, sizeof(temp));
        emitf(generator, &generator->output, "    %s =%s %s %s, %s\n",
              name, qbe_type(result_type == TYPE_BOOL ? TYPE_BOOL : left.type),
              instruction, left.text, right.text);
        value.type = result_type == TYPE_BOOL ? TYPE_BOOL : left.type;
        snprintf(value.text, sizeof(value.text), "%s", name);
        return value;
    }
    case EXPR_CALL: {
        const ExprList *argument = expr->as.call.arguments;
        if (strcmp(expr->as.call.name, "len") == 0) {
            if (!argument || argument->next ||
                argument->expr->kind != EXPR_NAME) {
                fail_at(generator, expr->loc,
                        "len requires a global static array name");
                return value;
            }
            Variable *array = find_variable(generator->globals,
                                            argument->expr->as.name);
            if (!array || array->type != TYPE_ARRAY_STRING) {
                fail_at(generator, expr->loc,
                        "len requires a global static array name");
                return value;
            }
            value.type = TYPE_INT;
            snprintf(value.text, sizeof(value.text), "%zu",
                     array->array_length);
            return value;
        }
        if (strcmp(expr->as.call.name, "str_at") == 0) {
            if (!argument || !argument->next || argument->next->next) {
                fail_at(generator, expr->loc,
                        "str_at expects a string and an index");
                return value;
            }
            Value string = emit_expr(context, argument->expr);
            Value index = emit_expr(context, argument->next->expr);
            if (string.type != TYPE_STRING || index.type != TYPE_INT) {
                fail_at(generator, expr->loc,
                        "str_at expects a string and an int index");
                return value;
            }
            char address_temp[64], byte_temp[64], extended_temp[64];
            const char *address = new_temp(generator, address_temp,
                                           sizeof(address_temp));
            const char *byte = new_temp(generator, byte_temp,
                                        sizeof(byte_temp));
            const char *extended = new_temp(generator, extended_temp,
                                            sizeof(extended_temp));
            emitf(generator, &generator->output,
                  "    %s =l add %s, %s\n"
                  "    %s =w loadub %s\n"
                  "    %s =l extuw %s\n",
                  address, string.text, index.text, byte, address,
                  extended, byte);
            value.type = TYPE_INT;
            snprintf(value.text, sizeof(value.text), "%s", extended);
            return value;
        }
        const Function *function = find_function(context->program,
                                                 expr->as.call.name);
        if (!function) {
            fail_at(generator, expr->loc, "unknown function");
            return value;
        }
        const Param *parameter = function->parameters;
        const ExprList *argument_cursor = expr->as.call.arguments;
        Buffer args = {0};
        while (parameter && argument_cursor && !generator->failed) {
            Value item = emit_expr(context, argument_cursor->expr);
            if (item.type != parameter->type) {
                fail_at(generator, argument_cursor->expr->loc,
                        "argument type does not match parameter");
                break;
            }
            if (!appendf(&args, "%s%s %s", args.length ? ", " : "",
                         qbe_type(item.type), item.text)) {
                generator->failed = 1;
                break;
            }
            parameter = parameter->next;
            argument_cursor = argument_cursor->next;
        }
        if (parameter || argument_cursor) {
            fail_at(generator, expr->loc,
                    "incorrect number of function arguments");
        }
        if (!generator->failed) {
            if (function->return_type == TYPE_VOID) {
                emitf(generator, &generator->output, "    call $%s(%s)\n",
                      function->name, args.data ? args.data : "");
                value.type = TYPE_VOID;
            } else {
                char temp[64];
                const char *name = new_temp(generator, temp, sizeof(temp));
                emitf(generator, &generator->output,
                      "    %s =%s call $%s(%s)\n", name,
                      qbe_type(function->return_type), function->name,
                      args.data ? args.data : "");
                value.type = function->return_type;
                snprintf(value.text, sizeof(value.text), "%s", name);
            }
        }
        free(args.data);
        return value;
    }
    case EXPR_KEY:
        fail_at(generator, expr->loc,
                "key input is only available for the web target");
        return value;
    }
    return value;
}

static size_t emit_string_literal(FunctionContext *context, const Expr *expr) {
    size_t id = context->generator->string_id++;
    if (!decode_string(context->generator, expr, id)) {
        context->generator->failed = 1;
    }
    return id;
}

static int emit_statements(FunctionContext *context, const Stmt *statements);

static int emit_if(FunctionContext *context, const Stmt *stmt) {
    Generator *generator = context->generator;
    Value condition = emit_expr(context, stmt->as.if_stmt.condition);
    if (condition.type != TYPE_BOOL) {
        fail_at(generator, stmt->loc, "if condition must be bool");
        return 0;
    }
    size_t label = generator->label++;
    emitf(generator, &generator->output,
          "    jnz %s, @if_body%zu, @if_end%zu\n"
          "@if_body%zu\n",
          condition.text, label, label, label);
    emit_statements(context, stmt->as.if_stmt.body);
    emitf(generator, &generator->output,
          "    jmp @if_end%zu\n@if_end%zu\n", label, label);
    return !generator->failed;
}

static int emit_while(FunctionContext *context, const Stmt *stmt) {
    Generator *generator = context->generator;
    size_t label = generator->label++;
    emitf(generator, &generator->output, "@while_cond%zu\n", label);
    Value condition = emit_expr(context, stmt->as.while_stmt.condition);
    if (condition.type != TYPE_BOOL) {
        fail_at(generator, stmt->loc, "while condition must be bool");
        return 0;
    }
    emitf(generator, &generator->output,
          "    jnz %s, @while_body%zu, @while_end%zu\n"
          "@while_body%zu\n",
          condition.text, label, label, label);
    emit_statements(context, stmt->as.while_stmt.body);
    emitf(generator, &generator->output,
          "    jmp @while_cond%zu\n@while_end%zu\n", label, label);
    return !generator->failed;
}

static int emit_statements(FunctionContext *context, const Stmt *statements) {
    Generator *generator = context->generator;
    for (const Stmt *stmt = statements; stmt && !generator->failed;
         stmt = stmt->next) {
        switch (stmt->kind) {
        case STMT_VAR: {
            Variable *variable =
                find_context_variable(context, stmt->as.var.name);
            if (!variable) {
                fail_at(generator, stmt->loc, "unknown variable declaration");
                return 0;
            }
            if (stmt->as.var.initializer) {
                Value value = emit_expr(context, stmt->as.var.initializer);
                if (value.type != variable->type) {
                    fail_at(generator, stmt->loc,
                            "initializer type does not match variable type");
                    return 0;
                }
                if (is_struct_type(variable->type)) {
                    fail_at(generator, stmt->loc,
                            "struct initializers are not supported");
                    return 0;
                }
                emitf(generator, &generator->output,
                      "    store%s %s, %%slot%zu\n", qbe_type(variable->type),
                      value.text, variable->slot);
            } else if (is_struct_type(variable->type)) {
                emit_zero_struct_slot(context, variable);
            } else {
                emitf(generator, &generator->output,
                      "    store%s 0, %%slot%zu\n", qbe_type(variable->type),
                      variable->slot);
            }
            break;
        }
        case STMT_ASSIGN: {
            Type target_type;
            if (!infer_expr_type(generator, context->program,
                                 context->variables, stmt->as.assign.target,
                                 &target_type)) {
                return 0;
            }
            Value value = emit_expr(context, stmt->as.assign.value);
            if (value.type != target_type) {
                fail_at(generator, stmt->loc,
                        "assigned value type does not match target type");
                return 0;
            }
            if (stmt->as.assign.target->kind == EXPR_NAME) {
                Variable *variable = find_context_variable(
                    context, stmt->as.assign.target->as.name);
                if (!variable) {
                    fail_at(generator, stmt->loc,
                            "assignment to unknown variable");
                    return 0;
                }
                if (is_struct_type(variable->type)) {
                    fail_at(generator, stmt->loc,
                            "whole-struct assignment is not supported");
                    return 0;
                }
                if (variable->global) {
                    emitf(generator, &generator->output,
                          "    store%s %s, $sourc_global_%s\n",
                          qbe_type(variable->type), value.text,
                          variable->name);
                } else {
                    emitf(generator, &generator->output,
                          "    store%s %s, %%slot%zu\n",
                          qbe_type(variable->type), value.text, variable->slot);
                }
            } else {
                char address[64];
                Type field_type;
                if (stmt->as.assign.target->kind != EXPR_FIELD ||
                    !emit_field_address(context, stmt->as.assign.target,
                                        &field_type, address, sizeof(address))) {
                    return 0;
                }
                const char *qtype = qbe_type(field_type);
                if (!qtype) {
                    fail_at(generator, stmt->loc,
                            "this struct field type is not supported by QBE");
                    return 0;
                }
                emitf(generator, &generator->output,
                      "    store%s %s, %s\n", qtype, value.text, address);
            }
            break;
        }
        case STMT_PRINT: {
            const Expr *expr = stmt->as.print_value;
            if (expr && expr->kind == EXPR_STRING) {
                size_t id = emit_string_literal(context, expr);
                if (id == 0) {
                    emitf(generator, &generator->output,
                          "    call $printf(l $fmt)\n");
                } else {
                    emitf(generator, &generator->output,
                          "    call $printf(l $str%zu)\n", id);
                }
                break;
            }
            Value value = emit_expr(context, expr);
            if (value.type == TYPE_INT) {
                emitf(generator, &generator->output,
                      "    call $printf(l $fmt_int, ..., l %s)\n", value.text);
            } else if (value.type == TYPE_BOOL) {
                emitf(generator, &generator->output,
                      "    call $printf(l $fmt_bool, ..., w %s)\n", value.text);
            } else if (value.type == TYPE_STRING) {
                emitf(generator, &generator->output,
                      "    call $printf(l $fmt_string, ..., l %s)\n", value.text);
            } else if (value.type == TYPE_FLOAT) {
                emitf(generator, &generator->output,
                      "    call $printf(l $fmt_float, ..., d %s)\n",
                      value.text);
            } else {
                fail_at(generator, stmt->loc, "print does not support this type");
                return 0;
            }
            break;
        }
        case STMT_SCREEN:
        case STMT_CLS:
        case STMT_RECT:
        case STMT_DRAW_RECT_FILLED:
        case STMT_DRAW_RECT_OUTLINE:
        case STMT_DRAW_LINE:
        case STMT_DRAW_GRADIENT_RECT:
        case STMT_CIRCLE:
        case STMT_FLIP:
        case STMT_DRAW_IMAGE:
        case STMT_DRAW_TEXT:
        case STMT_UI_TEXT:
        case STMT_UI_PANEL:
        case STMT_UI_CODE:
        case STMT_COPY_TO_CLIPBOARD:
        case STMT_PLAY_SOUND:
        case STMT_PLAY_MUSIC:
            fail_at(generator, stmt->loc,
                    "graphics primitives are only available for the web target");
            return 0;
        case STMT_IF:
            emit_if(context, stmt);
            break;
        case STMT_WHILE:
            emit_while(context, stmt);
            break;
        case STMT_ARENA:
        {
            ArenaScope scope = {
                .statement = stmt,
                .parent = generator->active_arena
            };
            ArenaScope *previous = generator->active_arena;
            generator->active_arena = &scope;
            emit_statements(context, stmt->as.arena_stmt.body);
            generator->active_arena = previous;
            break;
        }
        case STMT_RETURN:
            if (!context->function) {
                fail_at(generator, stmt->loc,
                        "return is only valid inside a function");
                return 0;
            }
            if (context->function->return_type == TYPE_VOID) {
                if (stmt->as.return_value) {
                    fail_at(generator, stmt->loc,
                            "void function cannot return a value");
                    return 0;
                }
                emitf(generator, &generator->output, "    ret\n");
            } else {
                if (!stmt->as.return_value) {
                    fail_at(generator, stmt->loc,
                            "non-void function must return a value");
                    return 0;
                }
                Value result = emit_expr(context, stmt->as.return_value);
                if (result.type != context->function->return_type) {
                    fail_at(generator, stmt->loc,
                            "return type does not match function result");
                    return 0;
                }
                emitf(generator, &generator->output, "    ret %s\n", result.text);
            }
            context->has_return = 1;
            emitf(generator, &generator->output, "@dead%zu\n",
                  generator->label++);
            break;
        case STMT_EXPR: {
            Value value = emit_expr(context, stmt->as.expression);
            if (value.type != TYPE_VOID) {
                fail_at(generator, stmt->loc,
                        "expression statement must be a void function call");
                return 0;
            }
            break;
        }
        }
    }
    return !generator->failed;
}

static int block_returns(const Stmt *statements) {
    for (const Stmt *stmt = statements; stmt; stmt = stmt->next) {
        if (stmt->kind == STMT_RETURN) {
            return 1;
        }
        if (stmt->kind == STMT_ARENA &&
            block_returns(stmt->as.arena_stmt.body)) {
            return 1;
        }
    }
    return 0;
}

static int emit_function(Generator *generator, const Program *program,
                         const Function *function) {
    FunctionContext context = {
        .generator = generator,
        .program = program,
        .function = function
    };
    for (const Param *parameter = function->parameters; parameter;
         parameter = parameter->next) {
        if (is_struct_type(parameter->type) ||
            parameter->type == TYPE_VOID || parameter->type == TYPE_INVALID ||
            !add_variable(generator, &context, parameter->name,
                          parameter->type, parameter->loc, NULL)) {
            if (is_struct_type(parameter->type)) {
                fail_at(generator, parameter->loc,
                        "QBE functions cannot pass structs by value");
            }
            goto cleanup;
        }
    }
    if (is_struct_type(function->return_type)) {
        fail_at(generator, function->loc,
                "QBE functions cannot return structs by value");
        goto cleanup;
    }
    if (!gather_variables(generator, &context, function->body, NULL)) {
        goto cleanup;
    }
    if (function->return_type != TYPE_VOID &&
        !block_returns(function->body)) {
        fail_at(generator, function->loc,
                "non-void function must contain a return statement");
        goto cleanup;
    }

    emitf(generator, &generator->output, "function ");
    if (function->return_type != TYPE_VOID) {
        emitf(generator, &generator->output, "%s ",
              qbe_type(function->return_type));
    }
    emitf(generator, &generator->output, "$%s(", function->name);
    size_t index = 0;
    for (const Param *parameter = function->parameters; parameter;
         parameter = parameter->next, index++) {
        emitf(generator, &generator->output, "%s%s %%arg%zu",
              index ? ", " : "", qbe_type(parameter->type), index);
    }
    emitf(generator, &generator->output, ") {\n@start\n");
    for (Variable *variable = context.variables; variable;
         variable = variable->next) {
        size_t slot_size = is_struct_type(variable->type)
                               ? type_size(program, variable->type)
                               : variable->type == TYPE_BOOL ? 4 : 8;
        size_t slot_alignment = variable->type == TYPE_BOOL ? 4 : 8;
        emitf(generator, &generator->output,
              "    %%slot%zu =l alloc%zu %zu\n", variable->slot,
              slot_alignment, slot_size);
    }
    index = 0;
    for (const Param *parameter = function->parameters; parameter;
         parameter = parameter->next, index++) {
        Variable *variable = find_variable(context.variables, parameter->name);
        emitf(generator, &generator->output, "    store%s %%arg%zu, %%slot%zu\n",
              qbe_type(parameter->type), index, variable->slot);
    }
    emit_statements(&context, function->body);
    if (!generator->failed) {
        if (function->return_type == TYPE_VOID) {
            emitf(generator, &generator->output, "    ret\n");
        } else if (function->return_type == TYPE_FLOAT) {
            emitf(generator, &generator->output,
                  "    ret d_0x0p+0\n");
        } else {
            emitf(generator, &generator->output, "    ret 0\n");
        }
        emitf(generator, &generator->output, "}\n\n");
    }

cleanup:
    while (context.variables) {
        Variable *next = context.variables->next;
        free(context.variables);
        context.variables = next;
    }
    return !generator->failed;
}

static int emit_main(Generator *generator, const Program *program) {
    FunctionContext context = {.generator = generator, .program = program};
    if (!gather_variables(generator, &context, program->body, NULL)) {
        goto cleanup;
    }
    emitf(generator, &generator->output, "export function w $main() {\n@start\n");
    for (Variable *variable = context.variables; variable;
         variable = variable->next) {
        size_t slot_size = is_struct_type(variable->type)
                               ? type_size(program, variable->type)
                               : variable->type == TYPE_BOOL ? 4 : 8;
        size_t slot_alignment = variable->type == TYPE_BOOL ? 4 : 8;
        emitf(generator, &generator->output,
              "    %%slot%zu =l alloc%zu %zu\n", variable->slot,
              slot_alignment, slot_size);
    }
    emit_statements(&context, program->body);
    if (!generator->failed) {
        emitf(generator, &generator->output, "    ret 0\n}\n");
    }

cleanup:
    while (context.variables) {
        Variable *next = context.variables->next;
        free(context.variables);
        context.variables = next;
    }
    return !generator->failed;
}

static void free_globals(Generator *generator) {
    while (generator->globals) {
        Variable *next = generator->globals->next;
        free(generator->globals);
        generator->globals = next;
    }
}

int codegen_qbe(const Program *program, char **output, size_t *length) {
    Generator generator = {.program = program};
    if (!emit_type_definitions(&generator)) {
        goto failed;
    }
    emitf(&generator, &generator.data,
          "data $fmt_int = { b \"%%lld\\n\\0\" }\n"
          "data $fmt_bool = { b \"%%d\\n\\0\" }\n"
          "data $fmt_float = { b \"%%g\\n\\0\" }\n"
          "data $fmt_string = { b \"%%s\\n\\0\" }\n\n");

    if (!gather_globals(&generator, program) ||
        !emit_globals(&generator, program)) {
        goto failed;
    }
    for (const Function *function = program->functions;
         function && !generator.failed; function = function->next) {
        for (const Function *other = function->next; other; other = other->next) {
            if (strcmp(function->name, other->name) == 0) {
                fail_at(&generator, other->loc, "duplicate function name");
                break;
            }
        }
        if (!generator.failed) {
            emit_function(&generator, program, function);
        }
    }
    if (!generator.failed) {
        emit_main(&generator, program);
    }
    if (generator.failed || !generator.output.data || !generator.data.data) {
        free_globals(&generator);
        free(generator.output.data);
        free(generator.data.data);
        if (!generator.failed) {
            fprintf(stderr, "out of memory while generating QBE IL\n");
        }
        return 0;
    }

    if (generator.data.length > SIZE_MAX - generator.output.length - 1) {
        free_globals(&generator);
        free(generator.output.data);
        free(generator.data.data);
        fprintf(stderr, "QBE IL output is too large\n");
        return 0;
    }
    size_t total = generator.data.length + generator.output.length + 1;
    char *result = malloc(total);
    if (!result) {
        free_globals(&generator);
        free(generator.output.data);
        free(generator.data.data);
        fprintf(stderr, "out of memory while generating QBE IL\n");
        return 0;
    }
    memcpy(result, generator.data.data, generator.data.length);
    memcpy(result + generator.data.length, generator.output.data,
           generator.output.length);
    result[total - 1] = '\0';
    *output = result;
    *length = total - 1;
    free_globals(&generator);
    free(generator.output.data);
    free(generator.data.data);
    return 1;

failed:
    free_globals(&generator);
    free(generator.output.data);
    free(generator.data.data);
    if (!generator.failed) {
        fprintf(stderr, "out of memory while generating QBE IL\n");
    }
    return 0;
}
