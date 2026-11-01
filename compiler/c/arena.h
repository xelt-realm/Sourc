#ifndef SOURC_ARENA_H
#define SOURC_ARENA_H

#include <stddef.h>

typedef struct ArenaBlock ArenaBlock;

typedef struct {
    ArenaBlock *head;
    size_t block_size;
} Arena;

void arena_init(Arena *arena, size_t block_size);
void *arena_alloc(Arena *arena, size_t size, size_t alignment);
void arena_destroy(Arena *arena);

#endif
