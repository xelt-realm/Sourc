#ifndef SOURC_COMPILER_H
#define SOURC_COMPILER_H

#include "ast.h"

#include <stddef.h>

typedef enum {
    TOK_EOF,
    TOK_NEWLINE,
    TOK_IDENT,
    TOK_INTEGER,
    TOK_FLOAT,
    TOK_STRING,
    TOK_LPAREN,
    TOK_RPAREN,
    TOK_LBRACKET,
    TOK_RBRACKET,
    TOK_COMMA,
    TOK_COLON,
    TOK_DOT,
    TOK_ARROW,
    TOK_PLUS,
    TOK_MINUS,
    TOK_STAR,
    TOK_SLASH,
    TOK_EQUAL,
    TOK_NOT_EQUAL,
    TOK_LESS,
    TOK_LESS_EQUAL,
    TOK_GREATER,
    TOK_GREATER_EQUAL
} TokenKind;

typedef struct {
    TokenKind kind;
    const char *text;
    size_t length;
    SourceLoc loc;
    long long integer;
    double floating;
} Token;

typedef struct {
    Token *items;
    size_t count;
    size_t capacity;
} TokenList;

int lexer_tokenize(const char *source, size_t length, TokenList *tokens);
void lexer_destroy(TokenList *tokens);
int parser_parse(const char *source, const TokenList *tokens, Ast *ast);
int codegen_qbe(const Program *program, char **output, size_t *length);

#endif
