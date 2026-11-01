#include "arena.h"

#include <stdint.h>
#include <stdlib.h>

struct ArenaBlock {
    ArenaBlock *next;
    size_t used;
    size_t capacity;
    max_align_t data[];
};

void arena_init(Arena *arena, size_t block_size) {
    arena->head = NULL;
    arena->block_size = block_size < 4096 ? 4096 : block_size;
}

void *arena_alloc(Arena *arena, size_t size, size_t alignment) {
    if (alignment == 0 || (alignment & (alignment - 1)) != 0) {
        return NULL;
    }
    if (size == 0) {
        size = 1;
    }

    ArenaBlock *block = arena->head;
    size_t offset = block ? (block->used + alignment - 1) & ~(alignment - 1) : 0;
    if (!block || offset > block->capacity || size > block->capacity - offset) {
        size_t capacity = arena->block_size;
        if (size > capacity) {
            capacity = size;
        }
        if (capacity > SIZE_MAX - sizeof(*block)) {
            return NULL;
        }
        block = malloc(sizeof(*block) + capacity);
        if (!block) {
            return NULL;
        }
        block->next = arena->head;
        block->used = 0;
        block->capacity = capacity;
        arena->head = block;
        offset = 0;
    }

    void *result = (unsigned char *)block->data + offset;
    block->used = offset + size;
    return result;
}

void arena_destroy(Arena *arena) {
    ArenaBlock *block = arena->head;
    while (block) {
        ArenaBlock *next = block->next;
        free(block);
        block = next;
    }
    arena->head = NULL;
}
