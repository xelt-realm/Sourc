#include "compiler.h"

#include <ctype.h>
#include <limits.h>
#include <stdalign.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const TokenList *tokens;
    size_t position;
    Ast *ast;
    int failed;
} Parser;

enum { OP_NEG = 1000 };

static Token *current(Parser *parser) {
    return &parser->tokens->items[parser->position];
}

static Token *advance(Parser *parser) {
    Token *token = current(parser);
    if (token->kind != TOK_EOF) {
        parser->position++;
    }
    return token;
}

static int word_is(const Token *token, const char *word) {
    size_t length = strlen(word);
    if (token->kind != TOK_IDENT || token->length != length) {
        return 0;
    }
    for (size_t i = 0; i < length; i++) {
        if (tolower((unsigned char)token->text[i]) !=
            tolower((unsigned char)word[i])) {
            return 0;
        }
    }
    return 1;
}

static int accept_word(Parser *parser, const char *word) {
    if (!word_is(current(parser), word)) {
        return 0;
    }
    advance(parser);
    return 1;
}

static int accept_kind(Parser *parser, TokenKind kind) {
    if (current(parser)->kind != kind) {
        return 0;
    }
    advance(parser);
    return 1;
}

static void parse_error(Parser *parser, const char *message) {
    if (!parser->failed) {
        Token *token = current(parser);
        fprintf(stderr, "%zu:%zu: %s\n", token->loc.line, token->loc.column,
                message);
        parser->failed = 1;
    }
}

static void *node_alloc(Parser *parser, size_t size, size_t alignment) {
    void *result = arena_alloc(&parser->ast->arena, size, alignment);
    if (!result) {
        parse_error(parser, "out of memory while building syntax tree");
        return NULL;
    }
    memset(result, 0, size);
    return result;
}

static const char *copy_text(Parser *parser, const char *text, size_t length) {
    char *copy = node_alloc(parser, length + 1, alignof(char));
    if (copy) {
        memcpy(copy, text, length);
        copy[length] = '\0';
    }
    return copy;
}

static const char *copy_token(Parser *parser, const Token *token) {
    return copy_text(parser, token->text, token->length);
}

static int consume_line_end(Parser *parser) {
    if (accept_kind(parser, TOK_NEWLINE)) {
        while (accept_kind(parser, TOK_NEWLINE)) {}
        return 1;
    }
    if (current(parser)->kind == TOK_EOF || word_is(current(parser), "end")) {
        return 1;
    }
    parse_error(parser, "expected end of line");
    return 0;
}

static const TypeDef *find_type(const Program *program, const char *name) {
    for (const TypeDef *type = program->types; type; type = type->next) {
        if (strcmp(type->name, name) == 0) {
            return type;
        }
    }
    return NULL;
}

static Type parse_type(Parser *parser) {
    Token *token = current(parser);
    if (word_is(token, "int")) {
        advance(parser);
        return TYPE_INT;
    }
    if (word_is(token, "bool")) {
        advance(parser);
        return TYPE_BOOL;
    }
    if (word_is(token, "string")) {
        advance(parser);
        return TYPE_STRING;
    }
    if (word_is(token, "float")) {
        advance(parser);
        return TYPE_FLOAT;
    }
    if (word_is(token, "array")) {
        advance(parser);
        if (!accept_kind(parser, TOK_LBRACKET)) {
            parse_error(parser, "expected '[' after array type");
            return TYPE_INVALID;
        }
        if (!word_is(current(parser), "string")) {
            parse_error(parser, "only array[string] is currently supported");
            return TYPE_INVALID;
        }
        advance(parser);
        if (!accept_kind(parser, TOK_RBRACKET)) {
            parse_error(parser, "expected ']' after array element type");
            return TYPE_INVALID;
        }
        return TYPE_ARRAY_STRING;
    }
    if (word_is(token, "void")) {
        advance(parser);
        return TYPE_VOID;
    }
    if (token->kind == TOK_IDENT) {
        const char *name = copy_token(parser, token);
        const TypeDef *type = find_type(&parser->ast->program, name);
        if (type) {
            advance(parser);
            return type->type_id;
        }
        parse_error(parser, "unknown type name");
        return TYPE_INVALID;
    }
    parse_error(parser,
                "expected type: int, float, bool, string, array[string], or void");
    return TYPE_INVALID;
}

static Expr *parse_expression(Parser *parser, int minimum_precedence);

static Expr *new_expr(Parser *parser, ExprKind kind, SourceLoc loc) {
    Expr *expr = node_alloc(parser, sizeof(*expr), alignof(Expr));
    if (expr) {
        expr->kind = kind;
        expr->loc = loc;
    }
    return expr;
}

static Expr *parse_postfix(Parser *parser, Expr *expr) {
    while (!parser->failed) {
        if (accept_kind(parser, TOK_LBRACKET)) {
            Expr *index = parse_expression(parser, 0);
            if (!accept_kind(parser, TOK_RBRACKET)) {
                parse_error(parser, "expected ']' after index");
                return expr;
            }
            Expr *indexed = new_expr(parser, EXPR_INDEX, expr->loc);
            if (!indexed) {
                return NULL;
            }
            indexed->as.index.array = expr;
            indexed->as.index.index = index;
            expr = indexed;
            continue;
        }
        if (accept_kind(parser, TOK_DOT)) {
            Token *name = current(parser);
            if (name->kind != TOK_IDENT) {
                parse_error(parser, "expected field name after '.'");
                return expr;
            }
            Expr *field = new_expr(parser, EXPR_FIELD, name->loc);
            if (!field) {
                return NULL;
            }
            field->as.field.object = expr;
            field->as.field.name = copy_token(parser, name);
            advance(parser);
            expr = field;
            continue;
        }
        break;
    }
    return expr;
}

static Expr *parse_primary(Parser *parser) {
    Token *token = current(parser);
    if (word_is(token, "true") || word_is(token, "false")) {
        Expr *expr = new_expr(parser, EXPR_BOOL, token->loc);
        if (expr) {
            expr->as.boolean = word_is(token, "true");
        }
        advance(parser);
        return expr;
    }
    if (token->kind == TOK_INTEGER) {
        Expr *expr = new_expr(parser, EXPR_INT, token->loc);
        if (expr) {
            expr->as.integer = token->integer;
        }
        advance(parser);
        return expr;
    }
    if (token->kind == TOK_FLOAT) {
        Expr *expr = new_expr(parser, EXPR_FLOAT, token->loc);
        if (expr) {
            expr->as.floating = token->floating;
        }
        advance(parser);
        return expr;
    }
    if (token->kind == TOK_STRING) {
        Expr *expr = new_expr(parser, EXPR_STRING, token->loc);
        if (expr) {
            expr->as.string.data = token->text;
            expr->as.string.length = token->length;
        }
        advance(parser);
        return expr;
    }
    if (accept_kind(parser, TOK_LPAREN)) {
        Expr *expr = parse_expression(parser, 0);
        if (!accept_kind(parser, TOK_RPAREN)) {
            parse_error(parser, "expected ')'");
        }
        return parse_postfix(parser, expr);
    }
    if (accept_kind(parser, TOK_LBRACKET)) {
        Expr *expr = new_expr(parser, EXPR_ARRAY, token->loc);
        if (!expr) {
            return NULL;
        }
        ExprList **tail = &expr->as.array;
        if (current(parser)->kind != TOK_RBRACKET) {
            do {
                ExprList *item = node_alloc(parser, sizeof(*item),
                                            alignof(ExprList));
                if (!item) {
                    return expr;
                }
                item->expr = parse_expression(parser, 0);
                *tail = item;
                tail = &item->next;
            } while (accept_kind(parser, TOK_COMMA));
        }
        if (!accept_kind(parser, TOK_RBRACKET)) {
            parse_error(parser, "expected ']' after array literal");
        }
        return expr;
    }
    if (token->kind == TOK_IDENT) {
        int is_key = word_is(token, "key");
        advance(parser);
        const char *name = copy_token(parser, token);
        if (accept_kind(parser, TOK_LPAREN)) {
            Expr *expr = new_expr(parser, EXPR_CALL, token->loc);
            if (!expr) {
                return NULL;
            }
            expr->as.call.name = name;
            ExprList **tail = &expr->as.call.arguments;
            if (current(parser)->kind != TOK_RPAREN) {
                do {
                    ExprList *item = node_alloc(parser, sizeof(*item),
                                                alignof(ExprList));
                    if (!item) {
                        return expr;
                    }
                    item->expr = parse_expression(parser, 0);
                    *tail = item;
                    tail = &item->next;
                } while (accept_kind(parser, TOK_COMMA));
            }
            if (!accept_kind(parser, TOK_RPAREN)) {
                parse_error(parser, "expected ')' after arguments");
            }
            if (is_key) {
                if (!expr->as.call.arguments ||
                    expr->as.call.arguments->next ||
                    !expr->as.call.arguments->expr ||
                    expr->as.call.arguments->expr->kind != EXPR_STRING) {
                    parse_error(parser, "key expects one string literal");
                    return NULL;
                }
                Expr *key = new_expr(parser, EXPR_KEY, token->loc);
                if (key) {
                    key->as.key = expr->as.call.arguments->expr;
                }
                return key;
            }
            return parse_postfix(parser, expr);
        }
        Expr *expr = new_expr(parser, EXPR_NAME, token->loc);
        if (expr) {
            expr->as.name = name;
        }
        return parse_postfix(parser, expr);
    }
    parse_error(parser, "expected expression");
    return NULL;
}

static int precedence(TokenKind kind) {
    switch (kind) {
    case TOK_EQUAL:
    case TOK_NOT_EQUAL:
    case TOK_LESS:
    case TOK_LESS_EQUAL:
    case TOK_GREATER:
    case TOK_GREATER_EQUAL:
        return 1;
    case TOK_PLUS:
    case TOK_MINUS:
        return 2;
    case TOK_STAR:
    case TOK_SLASH:
        return 3;
    default:
        return -1;
    }
}

static Expr *parse_expression(Parser *parser, int minimum_precedence) {
    Expr *left;
    if (accept_kind(parser, TOK_MINUS)) {
        Token *op = &parser->tokens->items[parser->position - 1];
        left = new_expr(parser, EXPR_UNARY, op->loc);
        if (left) {
            left->as.unary.op = OP_NEG;
            left->as.unary.operand = parse_expression(parser, 4);
        }
    } else {
        left = parse_primary(parser);
    }

    while (!parser->failed) {
        Token *op = current(parser);
        int op_precedence = precedence(op->kind);
        if (op_precedence < minimum_precedence) {
            break;
        }
        advance(parser);
        Expr *right = parse_expression(parser, op_precedence + 1);
        Expr *binary = new_expr(parser, EXPR_BINARY, op->loc);
        if (!binary) {
            return left;
        }
        binary->as.binary.op = op->kind;
        binary->as.binary.left = left;
        binary->as.binary.right = right;
        left = binary;
    }
    return left;
}

static Stmt *parse_statement(Parser *parser);

static Stmt *parse_block(Parser *parser) {
    Stmt *head = NULL;
    Stmt **tail = &head;
    while (!parser->failed && current(parser)->kind != TOK_EOF &&
           !word_is(current(parser), "end")) {
        if (accept_kind(parser, TOK_NEWLINE)) {
            continue;
        }
        Stmt *stmt = parse_statement(parser);
        if (!stmt) {
            break;
        }
        *tail = stmt;
        tail = &stmt->next;
    }
    if (!parser->failed && current(parser)->kind == TOK_EOF) {
        parse_error(parser, "expected 'end' before end of file");
    }
    return head;
}

static Stmt *new_stmt(Parser *parser, StmtKind kind, SourceLoc loc) {
    Stmt *stmt = node_alloc(parser, sizeof(*stmt), alignof(Stmt));
    if (stmt) {
        stmt->kind = kind;
        stmt->loc = loc;
    }
    return stmt;
}

static int parse_primitive_args_range(Parser *parser, Expr **arguments,
                                      size_t minimum, size_t maximum,
                                      const char *name) {
    if (!accept_kind(parser, TOK_LPAREN)) {
        parse_error(parser, "expected '(' after primitive name");
        return 0;
    }
    size_t count = 0;
    if (current(parser)->kind != TOK_RPAREN) {
        do {
            if (count >= maximum) {
                parse_error(parser, "too many primitive arguments");
                return 0;
            }
            arguments[count++] = parse_expression(parser, 0);
        } while (accept_kind(parser, TOK_COMMA));
    }
    if (!accept_kind(parser, TOK_RPAREN)) {
        parse_error(parser, "expected ')' after primitive arguments");
        return 0;
    }
    if (count < minimum) {
        char message[128];
        if (minimum == maximum) {
            snprintf(message, sizeof(message), "%s expects %zu argument(s)",
                     name, minimum);
        } else {
            snprintf(message, sizeof(message),
                     "%s expects between %zu and %zu argument(s)",
                     name, minimum, maximum);
        }
        parse_error(parser, message);
        return 0;
    }
    return !parser->failed;
}

static int parse_primitive_args(Parser *parser, Expr **arguments,
                                size_t expected, const char *name) {
    return parse_primitive_args_range(parser, arguments, expected, expected,
                                      name);
}

static Stmt *parse_statement(Parser *parser) {
    Token *start = current(parser);
    if (accept_word(parser, "var")) {
        Token *name = current(parser);
        if (name->kind != TOK_IDENT) {
            parse_error(parser, "expected variable name after 'var'");
            return NULL;
        }
        advance(parser);
        Stmt *stmt = new_stmt(parser, STMT_VAR, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.var.name = copy_token(parser, name);
        stmt->as.var.type = TYPE_INVALID;
        if (accept_kind(parser, TOK_COLON)) {
            stmt->as.var.type = parse_type(parser);
        }
        if (accept_kind(parser, TOK_EQUAL)) {
            stmt->as.var.initializer = parse_expression(parser, 0);
        }
        if (!stmt->as.var.initializer && stmt->as.var.type == TYPE_INVALID) {
            parse_error(parser, "inferred variable requires an initializer");
        }
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "screen")) {
        Expr *arguments[2] = {0};
        if (!parse_primitive_args(parser, arguments, 2, "screen")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_SCREEN, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.screen.width = arguments[0];
        stmt->as.screen.height = arguments[1];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "cls")) {
        Expr *arguments[1] = {0};
        if (!parse_primitive_args(parser, arguments, 0, "cls")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_CLS, start->loc);
        if (stmt) {
            consume_line_end(parser);
        }
        return stmt;
    }
    if (accept_word(parser, "rect")) {
        Expr *arguments[5] = {0};
        if (!parse_primitive_args(parser, arguments, 5, "rect")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_RECT, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.rect.x = arguments[0];
        stmt->as.rect.y = arguments[1];
        stmt->as.rect.width = arguments[2];
        stmt->as.rect.height = arguments[3];
        stmt->as.rect.color = arguments[4];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_rect_filled")) {
        Expr *arguments[5] = {0};
        if (!parse_primitive_args(parser, arguments, 5, "draw_rect_filled")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_RECT_FILLED, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.rect.x = arguments[0];
        stmt->as.rect.y = arguments[1];
        stmt->as.rect.width = arguments[2];
        stmt->as.rect.height = arguments[3];
        stmt->as.rect.color = arguments[4];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_rect_outline")) {
        Expr *arguments[6] = {0};
        if (!parse_primitive_args(parser, arguments, 6, "draw_rect_outline")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_RECT_OUTLINE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.rect_outline.x = arguments[0];
        stmt->as.rect_outline.y = arguments[1];
        stmt->as.rect_outline.width = arguments[2];
        stmt->as.rect_outline.height = arguments[3];
        stmt->as.rect_outline.color = arguments[4];
        stmt->as.rect_outline.thickness = arguments[5];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_line")) {
        Expr *arguments[5] = {0};
        if (!parse_primitive_args(parser, arguments, 5, "draw_line")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_LINE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.line.x1 = arguments[0];
        stmt->as.line.y1 = arguments[1];
        stmt->as.line.x2 = arguments[2];
        stmt->as.line.y2 = arguments[3];
        stmt->as.line.color = arguments[4];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_gradient_rect")) {
        Expr *arguments[6] = {0};
        if (!parse_primitive_args(parser, arguments, 6, "draw_gradient_rect")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_GRADIENT_RECT, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.gradient_rect.x = arguments[0];
        stmt->as.gradient_rect.y = arguments[1];
        stmt->as.gradient_rect.width = arguments[2];
        stmt->as.gradient_rect.height = arguments[3];
        stmt->as.gradient_rect.color1 = arguments[4];
        stmt->as.gradient_rect.color2 = arguments[5];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "circle")) {
        Expr *arguments[4] = {0};
        if (!parse_primitive_args(parser, arguments, 4, "circle")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_CIRCLE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.circle.x = arguments[0];
        stmt->as.circle.y = arguments[1];
        stmt->as.circle.radius = arguments[2];
        stmt->as.circle.color = arguments[3];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_image")) {
        Expr *arguments[3] = {0};
        if (!parse_primitive_args(parser, arguments, 3, "draw_image")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_IMAGE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.draw_image.path = arguments[0];
        stmt->as.draw_image.x = arguments[1];
        stmt->as.draw_image.y = arguments[2];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "draw_text")) {
        Expr *arguments[6] = {0};
        if (!parse_primitive_args_range(parser, arguments, 5, 6, "draw_text")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_DRAW_TEXT, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.draw_text.text = arguments[0];
        stmt->as.draw_text.x = arguments[1];
        stmt->as.draw_text.y = arguments[2];
        stmt->as.draw_text.size = arguments[3];
        stmt->as.draw_text.color = arguments[4];
        stmt->as.draw_text.font = arguments[5];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "ui_text")) {
        Expr *arguments[6] = {0};
        if (!parse_primitive_args(parser, arguments, 6, "ui_text")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_UI_TEXT, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.ui_text.text = arguments[0];
        stmt->as.ui_text.x = arguments[1];
        stmt->as.ui_text.y = arguments[2];
        stmt->as.ui_text.size = arguments[3];
        stmt->as.ui_text.color = arguments[4];
        stmt->as.ui_text.font = arguments[5];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "ui_panel")) {
        Expr *arguments[6] = {0};
        if (!parse_primitive_args(parser, arguments, 6, "ui_panel")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_UI_PANEL, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.ui_panel.x = arguments[0];
        stmt->as.ui_panel.y = arguments[1];
        stmt->as.ui_panel.width = arguments[2];
        stmt->as.ui_panel.height = arguments[3];
        stmt->as.ui_panel.color = arguments[4];
        stmt->as.ui_panel.radius = arguments[5];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "ui_code")) {
        Expr *arguments[5] = {0};
        if (!parse_primitive_args(parser, arguments, 5, "ui_code")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_UI_CODE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.ui_code.text = arguments[0];
        stmt->as.ui_code.x = arguments[1];
        stmt->as.ui_code.y = arguments[2];
        stmt->as.ui_code.width = arguments[3];
        stmt->as.ui_code.height = arguments[4];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "copy_to_clipboard")) {
        Expr *arguments[1] = {0};
        if (!parse_primitive_args(parser, arguments, 1, "copy_to_clipboard")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_COPY_TO_CLIPBOARD, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.expression = arguments[0];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "play_sound") ||
        accept_word(parser, "play_music")) {
        int is_music = word_is(start, "play_music");
        Expr *arguments[1] = {0};
        if (!parse_primitive_args(parser, arguments, 1,
                                  is_music ? "play_music" : "play_sound")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser,
                              is_music ? STMT_PLAY_MUSIC : STMT_PLAY_SOUND,
                              start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.audio_path = arguments[0];
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "flip")) {
        Expr *arguments[1] = {0};
        if (!parse_primitive_args(parser, arguments, 0, "flip")) {
            return NULL;
        }
        Stmt *stmt = new_stmt(parser, STMT_FLIP, start->loc);
        if (stmt) {
            consume_line_end(parser);
        }
        return stmt;
    }
    if (accept_word(parser, "print")) {
        Stmt *stmt = new_stmt(parser, STMT_PRINT, start->loc);
        if (!stmt) {
            return NULL;
        }
        if (!accept_kind(parser, TOK_LPAREN)) {
            parse_error(parser, "expected '(' after 'print'");
            return stmt;
        }
        stmt->as.print_value = parse_expression(parser, 0);
        if (!accept_kind(parser, TOK_RPAREN)) {
            parse_error(parser, "expected ')' after print argument");
        }
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "if")) {
        Stmt *stmt = new_stmt(parser, STMT_IF, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.if_stmt.condition = parse_expression(parser, 0);
        if (!accept_word(parser, "then")) {
            parse_error(parser, "expected 'then' after if condition");
            return stmt;
        }
        if (current(parser)->kind == TOK_NEWLINE) {
            consume_line_end(parser);
            stmt->as.if_stmt.body = parse_block(parser);
        } else {
            stmt->as.if_stmt.body = parse_statement(parser);
        }
        if (!accept_word(parser, "end")) {
            parse_error(parser, "expected 'end' for if block");
        }
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "while")) {
        Stmt *stmt = new_stmt(parser, STMT_WHILE, start->loc);
        if (!stmt) {
            return NULL;
        }
        stmt->as.while_stmt.condition = parse_expression(parser, 0);
        if (!accept_word(parser, "do")) {
            parse_error(parser, "expected 'do' after while condition");
            return stmt;
        }
        consume_line_end(parser);
        stmt->as.while_stmt.body = parse_block(parser);
        if (!accept_word(parser, "end")) {
            parse_error(parser, "expected 'end' for while block");
        }
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "arena")) {
        Stmt *stmt = new_stmt(parser, STMT_ARENA, start->loc);
        if (!stmt) {
            return NULL;
        }
        (void)accept_word(parser, "do");
        consume_line_end(parser);
        stmt->as.arena_stmt.body = parse_block(parser);
        if (!accept_word(parser, "end")) {
            parse_error(parser, "expected 'end' for arena block");
        }
        consume_line_end(parser);
        return stmt;
    }
    if (accept_word(parser, "return")) {
        Stmt *stmt = new_stmt(parser, STMT_RETURN, start->loc);
        if (!stmt) {
            return NULL;
        }
        if (current(parser)->kind != TOK_NEWLINE &&
            current(parser)->kind != TOK_EOF) {
            stmt->as.return_value = parse_expression(parser, 0);
        }
        consume_line_end(parser);
        return stmt;
    }
    if (start->kind == TOK_IDENT) {
        size_t lookahead = parser->position + 1;
        while (lookahead + 1 < parser->tokens->count &&
               parser->tokens->items[lookahead].kind == TOK_DOT &&
               parser->tokens->items[lookahead + 1].kind == TOK_IDENT) {
            lookahead += 2;
        }
        if (lookahead < parser->tokens->count &&
            parser->tokens->items[lookahead].kind == TOK_EQUAL) {
            Expr *target = parse_primary(parser);
            advance(parser);
            Stmt *stmt = new_stmt(parser, STMT_ASSIGN, start->loc);
            if (!stmt) {
                return NULL;
            }
            stmt->as.assign.target = target;
            stmt->as.assign.value = parse_expression(parser, 0);
            consume_line_end(parser);
            return stmt;
        }
    }

    Stmt *stmt = new_stmt(parser, STMT_EXPR, start->loc);
    if (!stmt) {
        return NULL;
    }
    stmt->as.expression = parse_expression(parser, 0);
    consume_line_end(parser);
    return stmt;
}

static TypeDef *parse_type_definition(Parser *parser) {
    Token *start = advance(parser);
    Token *name = current(parser);
    if (name->kind != TOK_IDENT) {
        parse_error(parser, "expected type name after 'type'");
        return NULL;
    }
    const char *type_name = copy_token(parser, name);
    if (find_type(&parser->ast->program, type_name)) {
        parse_error(parser, "duplicate type name");
        return NULL;
    }
    size_t count = 0;
    for (const TypeDef *type = parser->ast->program.types; type;
         type = type->next) {
        count++;
    }
    if (count > (size_t)(INT_MAX - TYPE_STRUCT_BASE)) {
        parse_error(parser, "too many custom types");
        return NULL;
    }
    TypeDef *definition = node_alloc(parser, sizeof(*definition),
                                     alignof(TypeDef));
    if (!definition) {
        return NULL;
    }
    definition->name = type_name;
    definition->type_id = TYPE_STRUCT_BASE + (Type)count;
    definition->loc = start->loc;
    advance(parser);
    consume_line_end(parser);

    TypeField **tail = &definition->fields;
    while (!parser->failed && current(parser)->kind != TOK_EOF &&
           !word_is(current(parser), "end")) {
        if (accept_kind(parser, TOK_NEWLINE)) {
            continue;
        }
        Token *field_name = current(parser);
        if (field_name->kind != TOK_IDENT) {
            parse_error(parser, "expected field name");
            break;
        }
        advance(parser);
        if (!accept_kind(parser, TOK_COLON)) {
            parse_error(parser, "expected ':' after field name");
            break;
        }
        TypeField *field = node_alloc(parser, sizeof(*field),
                                      alignof(TypeField));
        if (!field) {
            break;
        }
        field->name = copy_token(parser, field_name);
        field->loc = field_name->loc;
        field->type = parse_type(parser);
        if (field->type == TYPE_ARRAY_STRING || field->type == TYPE_VOID ||
            field->type == TYPE_INVALID) {
            if (!parser->failed) {
                parse_error(parser,
                            "struct fields must have a non-array value type");
            }
            break;
        }
        for (TypeField *previous = definition->fields; previous;
             previous = previous->next) {
            if (strcmp(previous->name, field->name) == 0) {
                parse_error(parser, "duplicate field name");
                break;
            }
        }
        if (parser->failed) {
            break;
        }
        *tail = field;
        tail = &field->next;
        consume_line_end(parser);
    }
    if (!parser->failed && !definition->fields) {
        parse_error(parser, "type must declare at least one field");
    }
    if (!accept_word(parser, "end")) {
        parse_error(parser, "expected 'end' for type definition");
    }
    consume_line_end(parser);
    if (!parser->failed) {
        TypeDef **type_tail = &parser->ast->program.types;
        while (*type_tail) {
            type_tail = &(*type_tail)->next;
        }
        *type_tail = definition;
    }
    return definition;
}

static Function *parse_function(Parser *parser) {
    Token *start = advance(parser);
    Token *name = current(parser);
    if (name->kind != TOK_IDENT) {
        parse_error(parser, "expected function name");
        return NULL;
    }
    advance(parser);
    Function *function = node_alloc(parser, sizeof(*function), alignof(Function));
    if (!function) {
        return NULL;
    }
    function->name = copy_token(parser, name);
    function->loc = start->loc;
    function->return_type = TYPE_VOID;

    if (!accept_kind(parser, TOK_LPAREN)) {
        parse_error(parser, "expected '(' after function name");
        return function;
    }
    Param **tail = &function->parameters;
    if (current(parser)->kind != TOK_RPAREN) {
        do {
            Token *parameter_name = current(parser);
            if (parameter_name->kind != TOK_IDENT) {
                parse_error(parser, "expected parameter name");
                return function;
            }
            advance(parser);
            if (!accept_kind(parser, TOK_COLON)) {
                parse_error(parser, "expected ':' after parameter name");
                return function;
            }
            Param *parameter = node_alloc(parser, sizeof(*parameter),
                                          alignof(Param));
            if (!parameter) {
                return function;
            }
            parameter->name = copy_token(parser, parameter_name);
            parameter->type = parse_type(parser);
            parameter->loc = parameter_name->loc;
            *tail = parameter;
            tail = &parameter->next;
        } while (accept_kind(parser, TOK_COMMA));
    }
    if (!accept_kind(parser, TOK_RPAREN)) {
        parse_error(parser, "expected ')' after parameters");
        return function;
    }
    if (accept_kind(parser, TOK_ARROW)) {
        function->return_type = parse_type(parser);
    }
    consume_line_end(parser);
    function->body = parse_block(parser);
    if (!accept_word(parser, "end")) {
        parse_error(parser, "expected 'end' for function");
    }
    consume_line_end(parser);
    return function;
}

void ast_init(Ast *ast) {
    memset(ast, 0, sizeof(*ast));
    arena_init(&ast->arena, 8192);
}

void ast_destroy(Ast *ast) {
    arena_destroy(&ast->arena);
    memset(&ast->program, 0, sizeof(ast->program));
}

int parser_parse(const char *source, const TokenList *tokens, Ast *ast) {
    (void)source;
    Parser parser = {.tokens = tokens, .ast = ast};
    Stmt **global_tail = &ast->program.globals;
    Stmt **stmt_tail = &ast->program.body;
    Function **function_tail = &ast->program.functions;
    while (!parser.failed && current(&parser)->kind != TOK_EOF) {
        if (accept_kind(&parser, TOK_NEWLINE)) {
            continue;
        }
        if (word_is(current(&parser), "func")) {
            Function *function = parse_function(&parser);
            if (!function) {
                break;
            }
            *function_tail = function;
            function_tail = &function->next;
            continue;
        }
        if (word_is(current(&parser), "type")) {
            TypeDef *type = parse_type_definition(&parser);
            if (!type) {
                break;
            }
            continue;
        }
        if (word_is(current(&parser), "var")) {
            Stmt *global = parse_statement(&parser);
            if (!global) {
                break;
            }
            *global_tail = global;
            global_tail = &global->next;
            continue;
        }
        Stmt *stmt = parse_statement(&parser);
        if (!stmt) {
            break;
        }
        *stmt_tail = stmt;
        stmt_tail = &stmt->next;
    }
    return !parser.failed;
}
