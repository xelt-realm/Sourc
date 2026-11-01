#define _POSIX_C_SOURCE 200809L
#include "compiler.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *source;
    size_t length;
    size_t offset;
    size_t line;
    size_t column;
    TokenList *tokens;
} Lexer;

static int push_token(Lexer *lexer, TokenKind kind, size_t start, size_t length,
                      size_t line, size_t column, long long integer) {
    TokenList *tokens = lexer->tokens;
    if (tokens->count == tokens->capacity) {
        size_t capacity = tokens->capacity ? tokens->capacity * 2 : 128;
        if (capacity > SIZE_MAX / sizeof(*tokens->items)) {
            return 0;
        }
        Token *items = realloc(tokens->items, capacity * sizeof(*items));
        if (!items) {
            return 0;
        }
        tokens->items = items;
        tokens->capacity = capacity;
    }
    tokens->items[tokens->count++] = (Token){
        .kind = kind,
        .text = lexer->source + start,
        .length = length,
        .loc = {.offset = start, .line = line, .column = column},
        .integer = integer
    };
    return 1;
}

static char current(const Lexer *lexer) {
    return lexer->offset < lexer->length ? lexer->source[lexer->offset] : '\0';
}

static char peek(const Lexer *lexer, size_t distance) {
    size_t position = lexer->offset + distance;
    return position < lexer->length ? lexer->source[position] : '\0';
}

static void advance(Lexer *lexer) {
    if (current(lexer) == '\n') {
        lexer->line++;
        lexer->column = 1;
    } else {
        lexer->column++;
    }
    lexer->offset++;
}

static int add_simple(Lexer *lexer, TokenKind kind, size_t size) {
    size_t start = lexer->offset, line = lexer->line, column = lexer->column;
    for (size_t i = 0; i < size; i++) {
        advance(lexer);
    }
    return push_token(lexer, kind, start, size, line, column, 0);
}

static int lex_number(Lexer *lexer) {
    size_t start = lexer->offset, line = lexer->line, column = lexer->column;
    while (isdigit((unsigned char)current(lexer))) {
        advance(lexer);
    }
    int is_float = 0;
    if (current(lexer) == '.' &&
        isdigit((unsigned char)peek(lexer, 1))) {
        is_float = 1;
        advance(lexer);
        while (isdigit((unsigned char)current(lexer))) {
            advance(lexer);
        }
    }
    size_t length = lexer->offset - start;
    if (is_float) {
        char *literal = malloc(length + 1);
        if (!literal) {
            return 0;
        }
        memcpy(literal, lexer->source + start, length);
        literal[length] = '\0';
        char *end = NULL;
        errno = 0;
        double floating = strtod(literal, &end);
        int valid = end == literal + length && errno != ERANGE &&
                    isfinite(floating);
        free(literal);
        if (!valid) {
            fprintf(stderr, "%zu:%zu: invalid floating-point literal\n",
                    line, column);
            return 0;
        }
        if (!push_token(lexer, TOK_FLOAT, start, length, line, column, 0)) {
            return 0;
        }
        lexer->tokens->items[lexer->tokens->count - 1].floating = floating;
        return 1;
    }
    char *literal = malloc(length + 1);
    if (!literal) {
        return 0;
    }
    memcpy(literal, lexer->source + start, length);
    literal[length] = '\0';
    char *end = NULL;
    errno = 0;
    long long value = strtoll(literal, &end, 10);
    int valid = end == literal + length && errno != ERANGE;
    free(literal);
    if (!valid) {
        fprintf(stderr, "%zu:%zu: integer literal is out of range\n",
                line, column);
        return 0;
    }
    return push_token(lexer, TOK_INTEGER, start, length, line, column, value);
}

static int lex_string(Lexer *lexer) {
    size_t quote = lexer->offset, line = lexer->line, column = lexer->column;
    advance(lexer);
    size_t start = lexer->offset;
    while (lexer->offset < lexer->length && current(lexer) != '"') {
        if (current(lexer) == '\n') {
            fprintf(stderr, "%zu:%zu: newline in string literal\n", line, column);
            return 0;
        }
        if (current(lexer) == '\\') {
            advance(lexer);
            if (lexer->offset >= lexer->length) {
                break;
            }
        }
        advance(lexer);
    }
    if (current(lexer) != '"') {
        fprintf(stderr, "%zu:%zu: unterminated string literal\n", line, column);
        return 0;
    }
    size_t length = lexer->offset - start;
    advance(lexer);
    (void)quote;
    return push_token(lexer, TOK_STRING, start, length, line, column, 0);
}

int lexer_tokenize(const char *source, size_t length, TokenList *tokens) {
    Lexer lexer = {
        .source = source, .length = length, .line = 1, .column = 1,
        .tokens = tokens
    };
    while (lexer.offset < lexer.length) {
        char ch = current(&lexer);
        if (ch == ' ' || ch == '\t' || ch == '\r') {
            advance(&lexer);
            continue;
        }
        if (ch == '\n') {
            if (!add_simple(&lexer, TOK_NEWLINE, 1)) {
                goto out_of_memory;
            }
            continue;
        }
        if (ch == '/' && peek(&lexer, 1) == '/') {
            while (lexer.offset < lexer.length && current(&lexer) != '\n') {
                advance(&lexer);
            }
            continue;
        }
        if (isalpha((unsigned char)ch) || ch == '_') {
            size_t start = lexer.offset, line = lexer.line, column = lexer.column;
            do {
                advance(&lexer);
                ch = current(&lexer);
            } while (isalnum((unsigned char)ch) || ch == '_');
            if (!push_token(&lexer, TOK_IDENT, start, lexer.offset - start,
                            line, column, 0)) {
                goto out_of_memory;
            }
            continue;
        }
        if (isdigit((unsigned char)ch)) {
            if (!lex_number(&lexer)) {
                return 0;
            }
            continue;
        }
        if (ch == '"') {
            if (!lex_string(&lexer)) {
                return 0;
            }
            continue;
        }

        TokenKind kind;
        size_t size = 1;
        switch (ch) {
        case '(': kind = TOK_LPAREN; break;
        case ')': kind = TOK_RPAREN; break;
        case '[': kind = TOK_LBRACKET; break;
        case ']': kind = TOK_RBRACKET; break;
        case ',': kind = TOK_COMMA; break;
        case ':': kind = TOK_COLON; break;
        case '.': kind = TOK_DOT; break;
        case '+': kind = TOK_PLUS; break;
        case '-':
            if (peek(&lexer, 1) == '>') {
                kind = TOK_ARROW;
                size = 2;
            } else {
                kind = TOK_MINUS;
            }
            break;
        case '*': kind = TOK_STAR; break;
        case '/': kind = TOK_SLASH; break;
        case '=': kind = TOK_EQUAL; break;
        case '!':
            if (peek(&lexer, 1) != '=') {
                fprintf(stderr, "%zu:%zu: unexpected '!'\n", lexer.line,
                        lexer.column);
                return 0;
            }
            kind = TOK_NOT_EQUAL;
            size = 2;
            break;
        case '<':
            if (peek(&lexer, 1) == '=') {
                kind = TOK_LESS_EQUAL;
                size = 2;
            } else if (peek(&lexer, 1) == '>') {
                kind = TOK_NOT_EQUAL;
                size = 2;
            } else {
                kind = TOK_LESS;
            }
            break;
        case '>':
            if (peek(&lexer, 1) == '=') {
                kind = TOK_GREATER_EQUAL;
                size = 2;
            } else {
                kind = TOK_GREATER;
            }
            break;
        default:
            fprintf(stderr, "%zu:%zu: unexpected character '%c'\n",
                    lexer.line, lexer.column, ch);
            return 0;
        }
        if (!add_simple(&lexer, kind, size)) {
            goto out_of_memory;
        }
    }
    if (!push_token(&lexer, TOK_EOF, lexer.offset, 0, lexer.line, lexer.column, 0)) {
        goto out_of_memory;
    }
    return 1;

out_of_memory:
    fprintf(stderr, "out of memory while tokenizing source\n");
    return 0;
}

void lexer_destroy(TokenList *tokens) {
    free(tokens->items);
    tokens->items = NULL;
    tokens->count = 0;
    tokens->capacity = 0;
}
