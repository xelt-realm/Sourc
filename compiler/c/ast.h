#ifndef SOURC_AST_H
#define SOURC_AST_H

#include "arena.h"

#include <stddef.h>

typedef struct {
    size_t offset;
    size_t line;
    size_t column;
} SourceLoc;

typedef enum {
    TYPE_INT,
    TYPE_BOOL,
    TYPE_STRING,
    TYPE_ARRAY_STRING,
    TYPE_FLOAT,
    TYPE_VOID,
    TYPE_INVALID,
    TYPE_STRUCT_BASE = 100
} Type;

typedef enum {
    EXPR_INT,
    EXPR_FLOAT,
    EXPR_BOOL,
    EXPR_STRING,
    EXPR_NAME,
    EXPR_UNARY,
    EXPR_BINARY,
    EXPR_CALL,
    EXPR_KEY,
    EXPR_ARRAY,
    EXPR_INDEX,
    EXPR_FIELD
} ExprKind;

typedef struct Expr Expr;
typedef struct Stmt Stmt;
typedef struct Param Param;
typedef struct Function Function;
typedef struct TypeDef TypeDef;
typedef struct TypeField TypeField;

typedef struct ExprList {
    Expr *expr;
    struct ExprList *next;
} ExprList;

struct Expr {
    ExprKind kind;
    SourceLoc loc;
    union {
        long long integer;
        double floating;
        int boolean;
        struct {
            const char *data;
            size_t length;
        } string;
        const char *name;
        struct {
            int op;
            Expr *operand;
        } unary;
        struct {
            int op;
            Expr *left;
            Expr *right;
        } binary;
        struct {
            const char *name;
            ExprList *arguments;
        } call;
        Expr *key;
        ExprList *array;
        struct {
            Expr *array;
            Expr *index;
        } index;
        struct {
            Expr *object;
            const char *name;
        } field;
    } as;
};

typedef enum {
    STMT_VAR,
    STMT_ASSIGN,
    STMT_PRINT,
    STMT_SCREEN,
    STMT_CLS,
    STMT_RECT,
    STMT_DRAW_RECT_FILLED,
    STMT_DRAW_RECT_OUTLINE,
    STMT_DRAW_LINE,
    STMT_DRAW_GRADIENT_RECT,
    STMT_CIRCLE,
    STMT_FLIP,
    STMT_DRAW_IMAGE,
    STMT_DRAW_TEXT,
    STMT_UI_TEXT,
    STMT_UI_PANEL,
    STMT_UI_CODE,
    STMT_COPY_TO_CLIPBOARD,
    STMT_PLAY_SOUND,
    STMT_PLAY_MUSIC,
    STMT_IF,
    STMT_WHILE,
    STMT_ARENA,
    STMT_RETURN,
    STMT_EXPR
} StmtKind;

struct Stmt {
    StmtKind kind;
    SourceLoc loc;
    Stmt *next;
    union {
        struct {
            const char *name;
            Type type;
            Expr *initializer;
        } var;
        struct {
            Expr *target;
            Expr *value;
        } assign;
        Expr *print_value;
        struct {
            Expr *width;
            Expr *height;
        } screen;
        struct {
            Expr *x;
            Expr *y;
            Expr *width;
            Expr *height;
            Expr *color;
        } rect;
        struct {
            Expr *x;
            Expr *y;
            Expr *width;
            Expr *height;
            Expr *color;
            Expr *thickness;
        } rect_outline;
        struct {
            Expr *x1;
            Expr *y1;
            Expr *x2;
            Expr *y2;
            Expr *color;
        } line;
        struct {
            Expr *x;
            Expr *y;
            Expr *width;
            Expr *height;
            Expr *color1;
            Expr *color2;
        } gradient_rect;
        struct {
            Expr *x;
            Expr *y;
            Expr *radius;
            Expr *color;
        } circle;
        struct {
            Expr *path;
            Expr *x;
            Expr *y;
        } draw_image;
        struct {
            Expr *text;
            Expr *x;
            Expr *y;
            Expr *size;
            Expr *color;
            Expr *font;
        } draw_text;
        struct {
            Expr *text;
            Expr *x;
            Expr *y;
            Expr *size;
            Expr *color;
            Expr *font;
        } ui_text;
        struct {
            Expr *x;
            Expr *y;
            Expr *width;
            Expr *height;
            Expr *color;
            Expr *radius;
        } ui_panel;
        struct {
            Expr *text;
            Expr *x;
            Expr *y;
            Expr *width;
            Expr *height;
        } ui_code;
        Expr *audio_path;
        struct {
            Expr *condition;
            Stmt *body;
        } if_stmt;
        struct {
            Expr *condition;
            Stmt *body;
        } while_stmt;
        struct {
            Stmt *body;
        } arena_stmt;
        Expr *return_value;
        Expr *expression;
    } as;
};

struct Param {
    const char *name;
    Type type;
    SourceLoc loc;
    Param *next;
};

struct Function {
    const char *name;
    Type return_type;
    Param *parameters;
    Stmt *body;
    SourceLoc loc;
    Function *next;
};

struct TypeField {
    const char *name;
    Type type;
    SourceLoc loc;
    TypeField *next;
};

struct TypeDef {
    const char *name;
    Type type_id;
    TypeField *fields;
    SourceLoc loc;
    TypeDef *next;
};

typedef struct {
    Stmt *globals;
    Stmt *body;
    Function *functions;
    TypeDef *types;
} Program;

typedef struct {
    Arena arena;
    Program program;
} Ast;

void ast_init(Ast *ast);
void ast_destroy(Ast *ast);

#endif
