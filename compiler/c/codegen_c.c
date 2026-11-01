#include "codegen_c.h"
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
    int failed;
} Buffer;

typedef struct Variable Variable;
struct Variable {
    const char *name;
    Type type;
    size_t array_length;
    const Stmt *arena_owner;
    Variable *next;
};

typedef struct ArenaScope ArenaScope;
struct ArenaScope {
    const Stmt *statement;
    ArenaScope *parent;
    size_t id;
};

typedef struct {
    Buffer output;
    const Program *program;
    Variable *variables;
    Variable *globals;
    const Function *function;
    const Stmt *frame_loop;
    ArenaScope *active_arena;
    size_t arena_id;
    int failed;
} Generator;

static void emitf(Generator *generator, const char *format, ...) {
    Buffer *buffer = &generator->output;
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

static const char *c_type(Type type) {
    switch (type) {
    case TYPE_INT: return "long long";
    case TYPE_FLOAT: return "double";
    case TYPE_BOOL: return "int";
    case TYPE_STRING: return "const char *";
    case TYPE_ARRAY_STRING: return NULL;
    case TYPE_VOID: return "void";
    default: return NULL;
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

static void emit_c_type(Generator *generator, Type type) {
    if (is_struct_type(type)) {
        const TypeDef *definition = find_type_by_id(generator->program, type);
        if (!definition) {
            generator->failed = 1;
            return;
        }
        emitf(generator, "sourc_t_%s", definition->name);
        return;
    }
    const char *name = c_type(type);
    if (!name) {
        generator->failed = 1;
        return;
    }
    emitf(generator, "%s", name);
}

static int valid_variable_type(Type type) {
    return c_type(type) != NULL || is_struct_type(type);
}

static int arena_contains(const ArenaScope *scope, const Stmt *owner) {
    for (; scope; scope = scope->parent) {
        if (scope->statement == owner) {
            return 1;
        }
    }
    return 0;
}

static int is_numeric_type(Type type) {
    return type == TYPE_INT || type == TYPE_FLOAT;
}

static void emit_var_name(Generator *generator, const char *name) {
    emitf(generator, "sourc_v_%s", name);
}

static void emit_function_name(Generator *generator, const char *name) {
    emitf(generator, "sourc_fn_%s", name);
}

static int find_parameter(const Param *parameters, const char *name) {
    for (const Param *parameter = parameters; parameter;
         parameter = parameter->next) {
        if (strcmp(parameter->name, name) == 0) {
            return 1;
        }
    }
    return 0;
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

static Variable *find_scoped_variable(Generator *generator,
                                      Variable *variables,
                                      const char *name) {
    Variable *variable = find_variable(variables, name);
    return variable ? variable : find_variable(generator->globals, name);
}

static int infer_type(Generator *generator, Variable *variables,
                      const Expr *expr, Type *type) {
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
        Variable *variable = find_scoped_variable(generator, variables,
                                                  expr->as.name);
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
            if (!infer_type(generator, variables, item->expr, &element_type)) {
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
        if (!infer_type(generator, variables, expr->as.index.array,
                        &array_type) ||
            !infer_type(generator, variables, expr->as.index.index,
                        &index_type)) {
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
        if (!infer_type(generator, variables, expr->as.field.object,
                        &object_type)) {
            return 0;
        }
        const TypeDef *definition =
            find_type_by_id(generator->program, object_type);
        const TypeField *field =
            find_type_field(definition, expr->as.field.name);
        if (!field) {
            fail_at(generator, expr->loc, "unknown field for struct type");
            return 0;
        }
        *type = field->type;
        return 1;
    }
    case EXPR_UNARY: {
        Type operand;
        if (!infer_type(generator, variables, expr->as.unary.operand, &operand)) {
            return 0;
        }
        if (!is_numeric_type(operand)) {
            fail_at(generator, expr->loc, "unary '-' requires a number");
            return 0;
        }
        *type = operand;
        return 1;
    }
    case EXPR_BINARY: {
        Type left, right;
        if (!infer_type(generator, variables, expr->as.binary.left, &left) ||
            !infer_type(generator, variables, expr->as.binary.right, &right)) {
            return 0;
        }
        if (!is_numeric_type(left) || !is_numeric_type(right)) {
            fail_at(generator, expr->loc,
                    "operators require numeric operands");
            return 0;
        }
        int op = expr->as.binary.op;
        *type = op >= TOK_EQUAL ? TYPE_BOOL
                                : (left == TYPE_FLOAT || right == TYPE_FLOAT
                                       ? TYPE_FLOAT : TYPE_INT);
        return 1;
    }
    case EXPR_CALL: {
        const ExprList *argument = expr->as.call.arguments;
        if (strcmp(expr->as.call.name, "ui_button") == 0) {
            Type argument_types[6];
            size_t count = 0;
            for (const ExprList *item = argument; item; item = item->next) {
                if (count == 6) {
                    fail_at(generator, expr->loc,
                            "ui_button expects six arguments");
                    return 0;
                }
                if (!infer_type(generator, variables, item->expr,
                                &argument_types[count])) {
                    return 0;
                }
                count++;
            }
            if (count != 6 || argument_types[0] != TYPE_STRING ||
                argument_types[5] != TYPE_STRING ||
                !is_numeric_type(argument_types[1]) ||
                !is_numeric_type(argument_types[2]) ||
                !is_numeric_type(argument_types[3]) ||
                !is_numeric_type(argument_types[4])) {
                fail_at(generator, expr->loc,
                        "ui_button expects (string, number, number, number, number, string)");
                return 0;
            }
            *type = TYPE_BOOL;
            return 1;
        }
        if (strcmp(expr->as.call.name, "delta") == 0 ||
            strcmp(expr->as.call.name, "time") == 0) {
            if (argument) {
                fail_at(generator, expr->loc, "time primitive expects no arguments");
                return 0;
            }
            *type = TYPE_FLOAT;
            return 1;
        }
        if (strcmp(expr->as.call.name, "mouse_x") == 0 ||
            strcmp(expr->as.call.name, "mouse_y") == 0) {
            if (argument) {
                fail_at(generator, expr->loc, "mouse position expects no arguments");
                return 0;
            }
            *type = TYPE_FLOAT;
            return 1;
        }
        if (strcmp(expr->as.call.name, "mouse_down") == 0) {
            if (argument) {
                fail_at(generator, expr->loc, "mouse_down expects no arguments");
                return 0;
            }
            *type = TYPE_BOOL;
            return 1;
        }
        if (strcmp(expr->as.call.name, "random") == 0) {
            Type lower, upper;
            if (!argument || !argument->next || argument->next->next ||
                !infer_type(generator, variables, argument->expr, &lower) ||
                !infer_type(generator, variables, argument->next->expr,
                            &upper)) {
                if (!generator->failed) {
                    fail_at(generator, expr->loc,
                            "random expects two numeric arguments");
                }
                return 0;
            }
            if (!is_numeric_type(lower) || !is_numeric_type(upper)) {
                fail_at(generator, expr->loc,
                        "random expects two numeric arguments");
                return 0;
            }
            *type = TYPE_FLOAT;
            return 1;
        }
        if (strcmp(expr->as.call.name, "sin") == 0 ||
            strcmp(expr->as.call.name, "cos") == 0 ||
            strcmp(expr->as.call.name, "sqrt") == 0 ||
            strcmp(expr->as.call.name, "floor") == 0 ||
            strcmp(expr->as.call.name, "abs") == 0) {
            Type value_type;
            if (!argument || argument->next ||
                !infer_type(generator, variables, argument->expr,
                            &value_type)) {
                if (!generator->failed) {
                    fail_at(generator, expr->loc,
                            "math function expects one numeric argument");
                }
                return 0;
            }
            if (!is_numeric_type(value_type)) {
                fail_at(generator, expr->loc,
                        "math function expects one numeric argument");
                return 0;
            }
            *type = TYPE_FLOAT;
            return 1;
        }
        if (strcmp(expr->as.call.name, "len") == 0) {
            Type argument_type;
            if (!argument || argument->next ||
                !infer_type(generator, variables, argument->expr,
                            &argument_type)) {
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
                !infer_type(generator, variables, argument->expr,
                            &string_type) ||
                !infer_type(generator, variables, argument->next->expr,
                            &index_type)) {
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
        const Function *function = find_function(generator->program,
                                                 expr->as.call.name);
        if (!function) {
            fail_at(generator, expr->loc, "unknown function");
            return 0;
        }
        const Param *parameter = function->parameters;
        const ExprList *argument_cursor = expr->as.call.arguments;
        while (parameter && argument_cursor) {
            Type argument_type;
            if (!infer_type(generator, variables, argument_cursor->expr,
                            &argument_type)) {
                return 0;
            }
            if (argument_type != parameter->type) {
                fail_at(generator, argument_cursor->expr->loc,
                        "argument type does not match parameter");
                return 0;
            }
            parameter = parameter->next;
            argument_cursor = argument_cursor->next;
        }
        if (parameter || argument_cursor) {
            fail_at(generator, expr->loc,
                    "incorrect number of function arguments");
            return 0;
        }
        *type = function->return_type;
        return 1;
    }
    case EXPR_KEY: {
        Type key_type;
        if (!infer_type(generator, variables, expr->as.key, &key_type)) {
            return 0;
        }
        if (key_type != TYPE_STRING) {
            fail_at(generator, expr->loc, "key expects a string");
            return 0;
        }
        *type = TYPE_BOOL;
        return 1;
    }
    }
    return 0;
}

static int add_variable(Generator *generator, const char *name, Type type,
                        SourceLoc loc, const Stmt *arena_owner) {
    if (find_variable(generator->variables, name)) {
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
    variable->arena_owner = is_struct_type(type) ? arena_owner : NULL;
    variable->next = generator->variables;
    generator->variables = variable;
    return 1;
}

static int gather_variables(Generator *generator, const Stmt *statements,
                            ArenaScope *scope) {
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
        if (nested && !gather_variables(generator, nested, child_scope)) {
            return 0;
        }
        if (stmt->kind != STMT_VAR) {
            continue;
        }
        Type type = stmt->as.var.type;
        if (type == TYPE_INVALID &&
            !infer_type(generator, generator->variables,
                        stmt->as.var.initializer, &type)) {
            return 0;
        }
        if (!valid_variable_type(type) || type == TYPE_VOID ||
            type == TYPE_INVALID) {
            fail_at(generator, stmt->loc, "invalid variable type");
            return 0;
        }
        if (!add_variable(generator, stmt->as.var.name, type, stmt->loc,
                          scope ? scope->statement : NULL)) {
            return 0;
        }
    }
    generator->active_arena = scope;
    return !generator->failed;
}

static int gather_global_variables(Generator *generator) {
    for (const Stmt *stmt = generator->program->globals; stmt;
         stmt = stmt->next) {
        Type type = stmt->as.var.type;
        if (type == TYPE_INVALID &&
            !infer_type(generator, NULL, stmt->as.var.initializer, &type)) {
            return 0;
        }
        if ((!valid_variable_type(type) && type != TYPE_ARRAY_STRING) ||
            type == TYPE_VOID || type == TYPE_INVALID) {
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
                if (item->expr->kind != EXPR_STRING) {
                    fail_at(generator, item->expr->loc,
                            "global array elements must be string literals");
                    return 0;
                }
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
        variable->arena_owner = NULL;
        variable->array_length = array_length;
        variable->next = generator->globals;
        generator->globals = variable;
    }
    return !generator->failed;
}

static int gather_runtime_variables(Generator *generator,
                                    const Stmt *statements) {
    for (const Stmt *stmt = statements; stmt; stmt = stmt->next) {
        if (stmt == generator->frame_loop) {
            continue;
        }
        Stmt statement = *stmt;
        statement.next = NULL;
        if (!gather_variables(generator, &statement, NULL)) {
            return 0;
        }
    }
    return !generator->failed;
}

static const Stmt *find_frame_loop(Generator *generator) {
    const Stmt *frame_loop = NULL;
    for (const Stmt *stmt = generator->program->body; stmt; stmt = stmt->next) {
        if (stmt->kind != STMT_WHILE ||
            stmt->as.while_stmt.condition->kind != EXPR_BOOL ||
            !stmt->as.while_stmt.condition->as.boolean) {
            continue;
        }
        if (frame_loop) {
            fail_at(generator, stmt->loc,
                    "only one top-level 'while true' game loop is allowed");
            return NULL;
        }
        frame_loop = stmt;
    }
    return frame_loop;
}

static void emit_string(Generator *generator, const Expr *expr) {
    emitf(generator, "\"");
    for (size_t i = 0; i < expr->as.string.length; i++) {
        unsigned char ch = (unsigned char)expr->as.string.data[i];
        if (ch == '\\') {
            if (++i >= expr->as.string.length) {
                fail_at(generator, expr->loc, "incomplete string escape");
                return;
            }
            switch (expr->as.string.data[i]) {
            case 'n': emitf(generator, "\\n"); break;
            case 'r': emitf(generator, "\\r"); break;
            case 't': emitf(generator, "\\t"); break;
            case '0': emitf(generator, "\\0"); break;
            case '\\': emitf(generator, "\\\\"); break;
            case '"': emitf(generator, "\\\""); break;
            default:
                fail_at(generator, expr->loc, "unsupported string escape");
                return;
            }
        } else if (ch == '"') {
            emitf(generator, "\\\"");
        } else if (ch == '\n') {
            emitf(generator, "\\n");
        } else if (ch == '\r') {
            emitf(generator, "\\r");
        } else {
            emitf(generator, "%c", ch);
        }
    }
    emitf(generator, "\"");
}

static void emit_expr(Generator *generator, const Expr *expr);

static void emit_expr_list(Generator *generator, const ExprList *arguments) {
    int first = 1;
    for (const ExprList *argument = arguments; argument;
         argument = argument->next) {
        if (!first) {
            emitf(generator, ", ");
        }
        emit_expr(generator, argument->expr);
        first = 0;
    }
}

static void emit_expr(Generator *generator, const Expr *expr) {
    if (!expr || generator->failed) {
        return;
    }
    switch (expr->kind) {
    case EXPR_INT:
        emitf(generator, "%lldLL", expr->as.integer);
        break;
    case EXPR_FLOAT:
        emitf(generator, "((double)%.17g)", expr->as.floating);
        break;
    case EXPR_BOOL:
        emitf(generator, "%s", expr->as.boolean ? "1" : "0");
        break;
    case EXPR_STRING:
        emit_string(generator, expr);
        break;
    case EXPR_NAME:
    {
        Variable *variable = find_scoped_variable(
            generator, generator->variables, expr->as.name);
        if (variable && variable->arena_owner) {
            emitf(generator, "(*");
            emit_var_name(generator, expr->as.name);
            emitf(generator, ")");
        } else {
            emit_var_name(generator, expr->as.name);
        }
        break;
    }
    case EXPR_ARRAY:
        fail_at(generator, expr->loc,
                "array literals are only valid as global initializers");
        break;
    case EXPR_INDEX:
        emit_expr(generator, expr->as.index.array);
        emitf(generator, "[");
        emit_expr(generator, expr->as.index.index);
        emitf(generator, "]");
        break;
    case EXPR_FIELD:
        emit_expr(generator, expr->as.field.object);
        emitf(generator, ".sourc_f_%s", expr->as.field.name);
        break;
    case EXPR_UNARY:
        emitf(generator, "(-");
        emit_expr(generator, expr->as.unary.operand);
        emitf(generator, ")");
        break;
    case EXPR_BINARY: {
        static const char *const operators[] = {
            [TOK_PLUS] = "+", [TOK_MINUS] = "-", [TOK_STAR] = "*",
            [TOK_SLASH] = "/", [TOK_EQUAL] = "==",
            [TOK_NOT_EQUAL] = "!=", [TOK_LESS] = "<",
            [TOK_LESS_EQUAL] = "<=", [TOK_GREATER] = ">",
            [TOK_GREATER_EQUAL] = ">="
        };
        int op = expr->as.binary.op;
        if (op < 0 || (size_t)op >= sizeof(operators) / sizeof(operators[0]) ||
            !operators[op]) {
            fail_at(generator, expr->loc, "unsupported binary operator");
            return;
        }
        emitf(generator, "(");
        emit_expr(generator, expr->as.binary.left);
        emitf(generator, " %s ", operators[op]);
        emit_expr(generator, expr->as.binary.right);
        emitf(generator, ")");
        break;
    }
    case EXPR_CALL:
        if (strcmp(expr->as.call.name, "ui_button") == 0) {
            emitf(generator, "js_ui_button(");
            emit_expr_list(generator, expr->as.call.arguments);
            emitf(generator, ")");
            break;
        }
        if (strcmp(expr->as.call.name, "delta") == 0) {
            emitf(generator, "sourc_frame_delta");
            break;
        }
        if (strcmp(expr->as.call.name, "time") == 0) {
            emitf(generator, "sourc_elapsed_time");
            break;
        }
        if (strcmp(expr->as.call.name, "mouse_x") == 0) {
            emitf(generator, "js_mouse_x()");
            break;
        }
        if (strcmp(expr->as.call.name, "mouse_y") == 0) {
            emitf(generator, "js_mouse_y()");
            break;
        }
        if (strcmp(expr->as.call.name, "mouse_down") == 0) {
            emitf(generator, "js_mouse_down()");
            break;
        }
        if (strcmp(expr->as.call.name, "random") == 0) {
            emitf(generator, "js_random(");
            emit_expr_list(generator, expr->as.call.arguments);
            emitf(generator, ")");
            break;
        }
        if (strcmp(expr->as.call.name, "sin") == 0 ||
            strcmp(expr->as.call.name, "cos") == 0 ||
            strcmp(expr->as.call.name, "sqrt") == 0 ||
            strcmp(expr->as.call.name, "floor") == 0 ||
            strcmp(expr->as.call.name, "abs") == 0) {
            const char *name = expr->as.call.name;
            if (strcmp(name, "abs") == 0) {
                name = "js_abs";
            } else if (strcmp(name, "floor") == 0) {
                name = "js_floor";
            } else {
                static const char *const prefix = "js_";
                emitf(generator, "%s%s(", prefix, name);
                emit_expr(generator, expr->as.call.arguments->expr);
                emitf(generator, ")");
                break;
            }
            emitf(generator, "%s(", name);
            emit_expr(generator, expr->as.call.arguments->expr);
            emitf(generator, ")");
            break;
        }
        if (strcmp(expr->as.call.name, "len") == 0) {
            const ExprList *argument = expr->as.call.arguments;
            if (!argument || argument->next ||
                argument->expr->kind != EXPR_NAME) {
                fail_at(generator, expr->loc,
                        "len requires a global static array name");
                break;
            }
            const Expr *array = argument->expr;
            Variable *variable = find_variable(generator->globals,
                                               array->as.name);
            if (!variable || variable->type != TYPE_ARRAY_STRING) {
                fail_at(generator, expr->loc,
                        "len requires a global static array name");
                break;
            }
            emitf(generator, "%zuLL", variable->array_length);
            break;
        }
        if (strcmp(expr->as.call.name, "str_at") == 0) {
            const ExprList *argument = expr->as.call.arguments;
            if (!argument || !argument->next || argument->next->next) {
                fail_at(generator, expr->loc,
                        "str_at expects a string and an index");
                break;
            }
            emitf(generator, "sourc_str_at(");
            emit_expr(generator, argument->expr);
            emitf(generator, ", ");
            emit_expr(generator, argument->next->expr);
            emitf(generator, ")");
            break;
        }
        emit_function_name(generator, expr->as.call.name);
        emitf(generator, "(");
        emit_expr_list(generator, expr->as.call.arguments);
        emitf(generator, ")");
        break;
    case EXPR_KEY:
        emitf(generator, "js_key(");
        emit_expr(generator, expr->as.key);
        emitf(generator, ")");
        break;
    }
}

static void emit_statements(Generator *generator, const Stmt *statements);

static int emit_int_argument(Generator *generator, const Expr *expr) {
    Type type;
    if (!infer_type(generator, generator->variables, expr, &type)) {
        return 0;
    }
    if (!is_numeric_type(type)) {
        fail_at(generator, expr->loc,
                "graphics primitives require numeric arguments");
        return 0;
    }
    emit_expr(generator, expr);
    return !generator->failed;
}

static void emit_statements(Generator *generator, const Stmt *statements) {
    for (const Stmt *stmt = statements; stmt && !generator->failed;
         stmt = stmt->next) {
        if (stmt == generator->frame_loop) {
            continue;
        }
        switch (stmt->kind) {
        case STMT_VAR: {
            Variable *variable = find_scoped_variable(
                generator, generator->variables, stmt->as.var.name);
            if (!variable) {
                fail_at(generator, stmt->loc, "unknown variable declaration");
                return;
            }
            if (stmt->as.var.initializer) {
                Type type;
                if (!infer_type(generator, generator->variables,
                                stmt->as.var.initializer, &type) ||
                    type != variable->type) {
                    if (!generator->failed) {
                        fail_at(generator, stmt->loc,
                                "initializer type does not match variable type");
                    }
                    return;
                }
                if (is_struct_type(variable->type)) {
                    fail_at(generator, stmt->loc,
                            "struct initializers are not supported");
                    return;
                }
                emitf(generator, "  ");
                emit_var_name(generator, variable->name);
                emitf(generator, " = ");
                emit_expr(generator, stmt->as.var.initializer);
                emitf(generator, ";\n");
            } else if (variable->arena_owner) {
                ArenaScope *scope = generator->active_arena;
                while (scope && scope->statement != variable->arena_owner) {
                    scope = scope->parent;
                }
                if (!scope) {
                    fail_at(generator, stmt->loc,
                            "struct allocation is outside its arena");
                    return;
                }
                const TypeDef *definition =
                    find_type_by_id(generator->program, variable->type);
                emitf(generator, "  ");
                emit_var_name(generator, variable->name);
                emitf(generator,
                      " = sourc_arena_alloc(&sourc_arena_%zu, "
                      "sizeof(*sourc_v_%s), _Alignof(sourc_t_%s));\n"
                      "  if (!sourc_v_%s) __builtin_trap();\n"
                      "  *sourc_v_%s = (sourc_t_%s){0};\n",
                      scope->id, variable->name, definition->name,
                      variable->name, variable->name, definition->name);
            } else {
                emitf(generator, "  ");
                emit_var_name(generator, variable->name);
                if (is_struct_type(variable->type)) {
                    const TypeDef *definition =
                        find_type_by_id(generator->program, variable->type);
                    emitf(generator, " = (sourc_t_%s){0};\n",
                          definition->name);
                } else {
                    emitf(generator, " = 0;\n");
                }
            }
            break;
        }
        case STMT_ASSIGN: {
            Type target_type, value_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.assign.target, &target_type) ||
                !infer_type(generator, generator->variables,
                            stmt->as.assign.value, &value_type) ||
                target_type != value_type) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "assigned value type does not match target type");
                }
                return;
            }
            emitf(generator, "  ");
            emit_expr(generator, stmt->as.assign.target);
            emitf(generator, " = ");
            emit_expr(generator, stmt->as.assign.value);
            emitf(generator, ";\n");
            break;
        }
        case STMT_PRINT:
            {
                Type type;
                if (!infer_type(generator, generator->variables,
                                stmt->as.print_value, &type)) {
                    return;
                }
                if (type == TYPE_STRING) {
                    emitf(generator, "  js_print(");
                    emit_expr(generator, stmt->as.print_value);
                    emitf(generator, ");\n");
                } else if (type == TYPE_INT) {
                    emitf(generator, "  js_print_int(");
                    emit_expr(generator, stmt->as.print_value);
                    emitf(generator, ");\n");
                } else if (type == TYPE_BOOL) {
                    emitf(generator, "  js_print_bool(");
                    emit_expr(generator, stmt->as.print_value);
                    emitf(generator, ");\n");
                } else if (type == TYPE_FLOAT) {
                    emitf(generator, "  js_print_float(");
                    emit_expr(generator, stmt->as.print_value);
                    emitf(generator, ");\n");
                } else {
                    fail_at(generator, stmt->loc,
                            "print does not support this type");
                    return;
                }
            }
            break;
        case STMT_SCREEN:
            emitf(generator, "  js_screen(");
            emit_int_argument(generator, stmt->as.screen.width);
            emitf(generator, ", ");
            emit_int_argument(generator, stmt->as.screen.height);
            emitf(generator, ");\n");
            break;
        case STMT_CLS:
            emitf(generator, "  js_cls();\n");
            break;
        case STMT_RECT:
        case STMT_DRAW_RECT_FILLED:
            emitf(generator, stmt->kind == STMT_RECT
                                 ? "  js_rect((float)("
                                 : "  js_draw_rect_filled((float)(");
            emit_int_argument(generator, stmt->as.rect.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect.width);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect.height);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.rect.color);
            emitf(generator, "));\n");
            break;
        case STMT_DRAW_RECT_OUTLINE:
            emitf(generator, "  js_draw_rect_outline((float)(");
            emit_int_argument(generator, stmt->as.rect_outline.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect_outline.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect_outline.width);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect_outline.height);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.rect_outline.color);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.rect_outline.thickness);
            emitf(generator, "));\n");
            break;
        case STMT_DRAW_LINE:
            emitf(generator, "  js_draw_line((float)(");
            emit_int_argument(generator, stmt->as.line.x1);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.line.y1);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.line.x2);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.line.y2);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.line.color);
            emitf(generator, "));\n");
            break;
        case STMT_DRAW_GRADIENT_RECT:
            emitf(generator, "  js_draw_gradient_rect((float)(");
            emit_int_argument(generator, stmt->as.gradient_rect.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.gradient_rect.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.gradient_rect.width);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.gradient_rect.height);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.gradient_rect.color1);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.gradient_rect.color2);
            emitf(generator, "));\n");
            break;
        case STMT_CIRCLE:
            emitf(generator, "  js_circle((float)(");
            emit_int_argument(generator, stmt->as.circle.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.circle.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.circle.radius);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.circle.color);
            emitf(generator, "));\n");
            break;
        case STMT_DRAW_IMAGE: {
            Type path_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.draw_image.path, &path_type) ||
                path_type != TYPE_STRING) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "draw_image path must be a string");
                }
                return;
            }
            emitf(generator, "  js_draw_image(");
            emit_expr(generator, stmt->as.draw_image.path);
            emitf(generator, ", (float)(");
            emit_int_argument(generator, stmt->as.draw_image.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.draw_image.y);
            emitf(generator, "));\n");
            break;
        }
        case STMT_DRAW_TEXT: {
            Type text_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.draw_text.text, &text_type) ||
                (text_type != TYPE_STRING && !is_numeric_type(text_type))) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "draw_text content must be a string or number");
                }
                return;
            }
            if (stmt->as.draw_text.font) {
                Type font_type;
                if (!infer_type(generator, generator->variables,
                                stmt->as.draw_text.font, &font_type) ||
                    font_type != TYPE_STRING) {
                    if (!generator->failed) {
                        fail_at(generator, stmt->as.draw_text.font->loc,
                                "draw_text font must be a string");
                    }
                    return;
                }
            }
            emitf(generator, "  %s(",
                  text_type == TYPE_STRING ? "js_draw_text"
                                           : "js_draw_text_number");
            emit_expr(generator, stmt->as.draw_text.text);
            emitf(generator, ", (float)(");
            emit_int_argument(generator, stmt->as.draw_text.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.draw_text.y);
            emitf(generator, "), (int)(");
            emit_int_argument(generator, stmt->as.draw_text.size);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.draw_text.color);
            emitf(generator, "), ");
            if (stmt->as.draw_text.font) {
                emit_expr(generator, stmt->as.draw_text.font);
            } else {
                emitf(generator, "0");
            }
            emitf(generator, ");\n");
            break;
        }
        case STMT_UI_TEXT: {
            Type text_type, font_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.ui_text.text, &text_type) ||
                text_type != TYPE_STRING ||
                !infer_type(generator, generator->variables,
                            stmt->as.ui_text.font, &font_type) ||
                font_type != TYPE_STRING) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "ui_text expects text and font strings");
                }
                return;
            }
            emitf(generator, "  js_ui_text(");
            emit_expr(generator, stmt->as.ui_text.text);
            emitf(generator, ", (float)(");
            emit_int_argument(generator, stmt->as.ui_text.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_text.y);
            emitf(generator, "), (int)(");
            emit_int_argument(generator, stmt->as.ui_text.size);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.ui_text.color);
            emitf(generator, "), ");
            emit_expr(generator, stmt->as.ui_text.font);
            emitf(generator, ");\n");
            break;
        }
        case STMT_UI_PANEL:
            emitf(generator, "  js_ui_panel((float)(");
            emit_int_argument(generator, stmt->as.ui_panel.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_panel.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_panel.width);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_panel.height);
            emitf(generator, "), (unsigned int)(");
            emit_int_argument(generator, stmt->as.ui_panel.color);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_panel.radius);
            emitf(generator, "));\n");
            break;
        case STMT_UI_CODE: {
            Type text_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.ui_code.text, &text_type) ||
                text_type != TYPE_STRING) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "ui_code content must be a string");
                }
                return;
            }
            emitf(generator, "  js_ui_code(");
            emit_expr(generator, stmt->as.ui_code.text);
            emitf(generator, ", (float)(");
            emit_int_argument(generator, stmt->as.ui_code.x);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_code.y);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_code.width);
            emitf(generator, "), (float)(");
            emit_int_argument(generator, stmt->as.ui_code.height);
            emitf(generator, "));\n");
            break;
        }
        case STMT_COPY_TO_CLIPBOARD: {
            Type text_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.expression, &text_type) ||
                text_type != TYPE_STRING) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "copy_to_clipboard expects a string");
                }
                return;
            }
            emitf(generator, "  js_copy_to_clipboard(");
            emit_expr(generator, stmt->as.expression);
            emitf(generator, ");\n");
            break;
        }
        case STMT_PLAY_SOUND:
        case STMT_PLAY_MUSIC: {
            Type path_type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.audio_path, &path_type) ||
                path_type != TYPE_STRING) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "audio path must be a string");
                }
                return;
            }
            emitf(generator, "  %s(",
                  stmt->kind == STMT_PLAY_SOUND ? "js_play_sound"
                                                : "js_play_music");
            emit_expr(generator, stmt->as.audio_path);
            emitf(generator, ");\n");
            break;
        }
        case STMT_FLIP:
            emitf(generator, "  js_flip();\n");
            break;
        case STMT_IF: {
            Type type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.if_stmt.condition, &type) ||
                type != TYPE_BOOL) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc, "if condition must be bool");
                }
                return;
            }
            emitf(generator, "  if (");
            emit_expr(generator, stmt->as.if_stmt.condition);
            emitf(generator, ") {\n");
            emit_statements(generator, stmt->as.if_stmt.body);
            emitf(generator, "  }\n");
            break;
        }
        case STMT_WHILE: {
            Type type;
            if (!infer_type(generator, generator->variables,
                            stmt->as.while_stmt.condition, &type) ||
                type != TYPE_BOOL) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc, "while condition must be bool");
                }
                return;
            }
            emitf(generator, "  while (1) {\n    if (!(");
            emit_expr(generator, stmt->as.while_stmt.condition);
            emitf(generator, ")) break;\n");
            emit_statements(generator, stmt->as.while_stmt.body);
            emitf(generator, "  }\n");
            break;
        }
        case STMT_ARENA:
        {
            ArenaScope scope = {
                .statement = stmt,
                .parent = generator->active_arena,
                .id = ++generator->arena_id
            };
            emitf(generator, "  {\n    _Alignas(max_align_t) unsigned char "
                             "sourc_arena_storage_%zu[",
                  scope.id);
            int first = 1;
            for (Variable *variable = generator->variables; variable;
                 variable = variable->next) {
                if (variable->arena_owner != stmt) {
                    continue;
                }
                const TypeDef *definition =
                    find_type_by_id(generator->program, variable->type);
                emitf(generator, "%s(sizeof(sourc_t_%s) + "
                                 "_Alignof(sourc_t_%s) - 1)",
                      first ? "" : " + ", definition->name,
                      definition->name);
                first = 0;
            }
            if (first) {
                emitf(generator, "1");
            }
            emitf(generator,
                  "];\n"
                  "    SourcArena sourc_arena_%zu = {"
                  "sourc_arena_storage_%zu, "
                  "sizeof(sourc_arena_storage_%zu), 0};\n",
                  scope.id, scope.id, scope.id);
            ArenaScope *previous = generator->active_arena;
            generator->active_arena = &scope;
            emit_statements(generator, stmt->as.arena_stmt.body);
            emitf(generator, "    sourc_arena_%zu.used = 0;\n  }\n", scope.id);
            generator->active_arena = previous;
            break;
        }
        case STMT_RETURN:
            if (!generator->function) {
                fail_at(generator, stmt->loc,
                        "return is only valid inside a function");
                return;
            }
            if (generator->function->return_type == TYPE_VOID) {
                if (stmt->as.return_value) {
                    fail_at(generator, stmt->loc,
                            "void function cannot return a value");
                    return;
                }
                emitf(generator, "  return;\n");
            } else {
                Type type;
                if (!stmt->as.return_value ||
                    !infer_type(generator, generator->variables,
                                stmt->as.return_value, &type) ||
                    type != generator->function->return_type) {
                    if (!generator->failed) {
                        fail_at(generator, stmt->loc,
                                "return type does not match function result");
                    }
                    return;
                }
                emitf(generator, "  return ");
                emit_expr(generator, stmt->as.return_value);
                emitf(generator, ";\n");
            }
            break;
        case STMT_EXPR:
            emitf(generator, "  ");
            emit_expr(generator, stmt->as.expression);
            emitf(generator, ";\n");
            break;
        }
    }
}

static void free_variables(Generator *generator) {
    while (generator->variables) {
        Variable *next = generator->variables->next;
        free(generator->variables);
        generator->variables = next;
    }
}

static void free_globals(Generator *generator) {
    while (generator->globals) {
        Variable *next = generator->globals->next;
        free(generator->globals);
        generator->globals = next;
    }
}

static int emit_type_definitions(Generator *generator) {
    for (const TypeDef *type = generator->program->types; type;
         type = type->next) {
        emitf(generator, "typedef struct sourc_t_%s {\n", type->name);
        for (const TypeField *field = type->fields; field;
             field = field->next) {
            emitf(generator, "  ");
            emit_c_type(generator, field->type);
            emitf(generator, " sourc_f_%s;\n", field->name);
        }
        emitf(generator, "} sourc_t_%s;\n\n", type->name);
    }
    return !generator->failed;
}

static int emit_global_variables(Generator *generator) {
    for (const Stmt *stmt = generator->program->globals; stmt;
         stmt = stmt->next) {
        Variable *variable = find_variable(generator->globals,
                                           stmt->as.var.name);
        if (variable->type == TYPE_ARRAY_STRING) {
            emitf(generator, "static const char *");
            emit_var_name(generator, variable->name);
            emitf(generator, "[%zu] = {", variable->array_length);
            const ExprList *item = stmt->as.var.initializer->as.array;
            for (size_t index = 0; item; item = item->next, index++) {
                emitf(generator, "%s", index ? ", " : " ");
                emit_expr(generator, item->expr);
            }
            emitf(generator, " };\n");
            continue;
        }
        emitf(generator, "static ");
        emit_c_type(generator, variable->type);
        emitf(generator, " ");
        emit_var_name(generator, variable->name);
        if (stmt->as.var.initializer) {
            Type initializer_type;
            if (!infer_type(generator, NULL, stmt->as.var.initializer,
                            &initializer_type) ||
                initializer_type != variable->type) {
                if (!generator->failed) {
                    fail_at(generator, stmt->loc,
                            "initializer type does not match global variable type");
                }
                return 0;
            }
            emitf(generator, " = ");
            emit_expr(generator, stmt->as.var.initializer);
        } else if (is_struct_type(variable->type)) {
            const TypeDef *definition =
                find_type_by_id(generator->program, variable->type);
            emitf(generator, " = {0}");
            if (!definition) {
                generator->failed = 1;
            }
        } else {
            emitf(generator, " = 0");
        }
        emitf(generator, ";\n");
    }
    emitf(generator, "\n");
    return !generator->failed;
}

static void emit_function_signature(Generator *generator,
                                    const Function *function) {
    emit_c_type(generator, function->return_type);
    emitf(generator, " ");
    emit_function_name(generator, function->name);
    emitf(generator, "(");
    if (!function->parameters) {
        emitf(generator, "void");
    }
    size_t index = 0;
    for (const Param *parameter = function->parameters; parameter;
         parameter = parameter->next, index++) {
        if (index) {
            emitf(generator, ", ");
        }
        emit_c_type(generator, parameter->type);
        emitf(generator, " ");
        emit_var_name(generator, parameter->name);
    }
}

static void emit_function_prototype(Generator *generator,
                                    const Function *function) {
    emit_function_signature(generator, function);
    emitf(generator, ");\n");
}

static int emit_function_definition(Generator *generator,
                                    const Function *function) {
    generator->function = function;
    for (const Param *parameter = function->parameters; parameter;
         parameter = parameter->next) {
        if (!valid_variable_type(parameter->type) ||
            parameter->type == TYPE_VOID ||
            !add_variable(generator, parameter->name, parameter->type,
                          parameter->loc, NULL)) {
            free_variables(generator);
            return 0;
        }
    }
    if (!gather_variables(generator, function->body, NULL)) {
        free_variables(generator);
        return 0;
    }

    emit_function_signature(generator, function);
    emitf(generator, ") {\n");
    for (Variable *variable = generator->variables; variable;
         variable = variable->next) {
        if (find_parameter(function->parameters, variable->name)) {
            continue;
        }
        emitf(generator, "  ");
        emit_c_type(generator, variable->type);
        if (variable->arena_owner) {
            emitf(generator, " *");
        } else {
            emitf(generator, " ");
        }
        emit_var_name(generator, variable->name);
        if (variable->arena_owner) {
            emitf(generator, " = NULL;\n");
        } else if (is_struct_type(variable->type)) {
            emitf(generator, " = {0};\n");
        } else {
            emitf(generator, " = 0;\n");
        }
    }
    emit_statements(generator, function->body);
    if (function->return_type == TYPE_VOID) {
        emitf(generator, "  return;\n");
    } else if (!block_returns(function->body)) {
        emitf(generator, "  return 0;\n");
    }
    emitf(generator, "}\n\n");
    free_variables(generator);
    return !generator->failed;
}

static int emit_update(Generator *generator) {
    generator->function = NULL;
    generator->variables = NULL;
    if (generator->frame_loop &&
        !gather_variables(generator,
                          generator->frame_loop->as.while_stmt.body, NULL)) {
        free_variables(generator);
        return 0;
    }
    emitf(generator,
          "__attribute__((export_name(\"update\"))) void update(void) {\n");
    for (Variable *variable = generator->variables; variable;
         variable = variable->next) {
        emitf(generator, "  ");
        emit_c_type(generator, variable->type);
        if (variable->arena_owner) {
            emitf(generator, " *");
        } else {
            emitf(generator, " ");
        }
        emit_var_name(generator, variable->name);
        if (variable->arena_owner) {
            emitf(generator, " = NULL;\n");
        } else if (is_struct_type(variable->type)) {
            emitf(generator, " = {0};\n");
        } else {
            emitf(generator, " = 0;\n");
        }
    }
    if (generator->frame_loop) {
        emit_statements(generator, generator->frame_loop->as.while_stmt.body);
    }
    emitf(generator, "  return;\n}\n\n");
    free_variables(generator);
    return !generator->failed;
}

int codegen_c(const Program *program, char **output, size_t *length) {
    Generator generator = {.program = program};
    emitf(&generator,
          "#include <stddef.h>\n"
          "#include <stdint.h>\n"
          "typedef struct { unsigned char *memory; size_t capacity; "
          "size_t used; } SourcArena;\n"
          "static void *sourc_arena_alloc(SourcArena *arena, size_t size, "
          "size_t alignment) {\n"
          "  if (!alignment || (alignment & (alignment - 1)) || "
          "arena->used > arena->capacity) return NULL;\n"
          "  uintptr_t base = (uintptr_t)arena->memory;\n"
          "  if (base > UINTPTR_MAX - arena->used) return NULL;\n"
          "  uintptr_t current = base + arena->used;\n"
          "  if (current > UINTPTR_MAX - (alignment - 1)) return NULL;\n"
          "  uintptr_t aligned = (current + alignment - 1) & "
          "~(uintptr_t)(alignment - 1);\n"
          "  if (aligned < base) return NULL;\n"
          "  size_t offset = (size_t)(aligned - base);\n"
          "  if (offset > arena->capacity || "
          "size > arena->capacity - offset) return NULL;\n"
          "  arena->used = offset + size;\n"
          "  return (void *)aligned;\n"
          "}\n"
          "#define SOURC_IMPORT(name) "
          "__attribute__((import_module(\"env\"), import_name(name)))\n"
          "extern void js_print(const char *) SOURC_IMPORT(\"js_print\");\n"
          "extern void js_print_int(long long) "
          "SOURC_IMPORT(\"js_print_int\");\n"
          "extern void js_print_bool(int) SOURC_IMPORT(\"js_print_bool\");\n"
          "extern void js_print_float(double) "
          "SOURC_IMPORT(\"js_print_float\");\n"
          "extern void js_screen(int, int) SOURC_IMPORT(\"js_screen\");\n"
          "extern void js_cls(void) SOURC_IMPORT(\"js_cls\");\n"
          "extern void js_rect(float, float, float, float, unsigned int) "
          "SOURC_IMPORT(\"js_rect\");\n"
          "extern void js_draw_rect_filled(float, float, float, float, "
          "unsigned int) SOURC_IMPORT(\"js_draw_rect_filled\");\n"
          "extern void js_draw_rect_outline(float, float, float, float, "
          "unsigned int, float) SOURC_IMPORT(\"js_draw_rect_outline\");\n"
          "extern void js_draw_line(float, float, float, float, unsigned int) "
          "SOURC_IMPORT(\"js_draw_line\");\n"
          "extern void js_draw_gradient_rect(float, float, float, float, "
          "unsigned int, unsigned int) "
          "SOURC_IMPORT(\"js_draw_gradient_rect\");\n"
          "extern void js_circle(float, float, float, unsigned int) "
          "SOURC_IMPORT(\"js_circle\");\n"
          "extern void js_draw_image(const char *, float, float) "
          "SOURC_IMPORT(\"js_draw_image\");\n"
          "extern void js_flip(void) SOURC_IMPORT(\"js_flip\");\n"
          "extern int js_key(const char *) SOURC_IMPORT(\"js_key\");\n"
          "extern double js_random(double, double) "
          "SOURC_IMPORT(\"js_random\");\n"
          "extern double js_sin(double) SOURC_IMPORT(\"js_sin\");\n"
          "extern double js_cos(double) SOURC_IMPORT(\"js_cos\");\n"
          "extern double js_sqrt(double) SOURC_IMPORT(\"js_sqrt\");\n"
          "extern double js_floor(double) SOURC_IMPORT(\"js_floor\");\n"
          "extern double js_abs(double) SOURC_IMPORT(\"js_abs\");\n"
          "extern void js_draw_text(const char *, float, float, int, "
          "uint32_t, const char *) SOURC_IMPORT(\"js_draw_text\");\n"
          "extern void js_draw_text_number(double, float, float, int, "
          "uint32_t, const char *) SOURC_IMPORT(\"js_draw_text_number\");\n"
          "extern void js_ui_text(const char *, float, float, int, uint32_t, "
          "const char *) SOURC_IMPORT(\"js_ui_text\");\n"
          "extern int js_ui_button(const char *, float, float, float, float, "
          "const char *) SOURC_IMPORT(\"js_ui_button\");\n"
          "extern void js_ui_panel(float, float, float, float, uint32_t, "
          "float) SOURC_IMPORT(\"js_ui_panel\");\n"
          "extern void js_ui_code(const char *, float, float, float, float) "
          "SOURC_IMPORT(\"js_ui_code\");\n"
          "extern void js_copy_to_clipboard(const char *) "
          "SOURC_IMPORT(\"js_copy_to_clipboard\");\n"
          "extern void js_play_sound(const char *) "
          "SOURC_IMPORT(\"js_play_sound\");\n"
          "extern void js_play_music(const char *) "
          "SOURC_IMPORT(\"js_play_music\");\n"
          "extern double js_mouse_x(void) SOURC_IMPORT(\"js_mouse_x\");\n"
          "extern double js_mouse_y(void) SOURC_IMPORT(\"js_mouse_y\");\n"
          "extern int js_mouse_down(void) SOURC_IMPORT(\"js_mouse_down\");\n"
          "static double sourc_frame_delta;\n"
          "static double sourc_elapsed_time;\n"
          "__attribute__((export_name(\"sourc_set_frame_time\"))) "
          "void sourc_set_frame_time(double delta, double elapsed) {\n"
          "  sourc_frame_delta = delta;\n"
          "  sourc_elapsed_time = elapsed;\n"
          "}\n"
          "static long long sourc_str_at(const char *text, long long index) {\n"
          "  if (!text || index < 0) return -1;\n"
          "  for (long long i = 0; i < index; i++) {\n"
          "    if (text[i] == '\\0') return -1;\n"
          "  }\n"
          "  return text[index] ? (unsigned char)text[index] : -1;\n"
          "}\n\n");
    generator.frame_loop = find_frame_loop(&generator);
    if (generator.failed || !gather_global_variables(&generator) ||
        !emit_type_definitions(&generator) ||
        !emit_global_variables(&generator)) {
        goto failed;
    }
    for (const Function *function = program->functions; function;
         function = function->next) {
        for (const Function *other = function->next; other;
             other = other->next) {
            if (strcmp(function->name, other->name) == 0) {
                fail_at(&generator, other->loc, "duplicate function name");
                goto failed;
            }
        }
        emit_function_prototype(&generator, function);
    }
    emitf(&generator, "\n");
    for (const Function *function = program->functions;
         function && !generator.failed; function = function->next) {
        if (!emit_function_definition(&generator, function)) {
            goto failed;
        }
    }
    generator.function = NULL;
    if (!gather_runtime_variables(&generator, program->body)) {
        goto failed;
    }
    emitf(&generator, "int main(void) {\n");
    for (Variable *variable = generator.variables; variable;
         variable = variable->next) {
        emitf(&generator, "  ");
        emit_c_type(&generator, variable->type);
        if (variable->arena_owner) {
            emitf(&generator, " *");
        } else {
            emitf(&generator, " ");
        }
        emit_var_name(&generator, variable->name);
        if (variable->arena_owner) {
            emitf(&generator, " = NULL;\n");
        } else if (is_struct_type(variable->type)) {
            emitf(&generator, " = {0};\n");
        } else {
            emitf(&generator, " = 0;\n");
        }
    }
    emit_statements(&generator, program->body);
    emitf(&generator, "  return 0;\n}\n");
    free_variables(&generator);
    if (!emit_update(&generator)) {
        goto failed;
    }

    if (generator.failed || !generator.output.data) {
        goto failed;
    }
    *output = generator.output.data;
    *length = generator.output.length;
    free_globals(&generator);
    return 1;

failed:
    free_variables(&generator);
    free_globals(&generator);
    free(generator.output.data);
    return 0;
}
