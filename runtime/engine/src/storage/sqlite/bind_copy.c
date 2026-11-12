/* Keep SQLite's genuine transient destructor sentinel in C, where its declared
 * ABI permits the special -1 value. No Zig function-pointer alignment cast and
 * no borrowed-buffer lifetime substitution crosses this boundary. */
#include "sqlite3.h"
#include <limits.h>
#include <stddef.h>

extern void *lp_runtime_malloc(size_t);
extern void lp_runtime_c_free(void *);
extern void *lp_runtime_realloc(void *, size_t);
extern size_t lp_runtime_size(const void *);

static void *runtime_malloc(int size) {
    return size > 0 ? lp_runtime_malloc((size_t)size) : NULL;
}

static void *runtime_realloc(void *pointer, int size) {
    return size >= 0 ? lp_runtime_realloc(pointer, (size_t)size) : NULL;
}

static int runtime_size(void *pointer) {
    /* xMalloc/xRealloc receive positive int sizes, which the shared header
     * preserves exactly. No libc usable-size query or ownership guessing. */
    return (int)lp_runtime_size(pointer);
}

static int runtime_roundup(int size) {
    return size > 0 && size <= INT_MAX - 7 ? (size + 7) & ~7 : 0;
}

static int runtime_init(void *context) {
    (void)context;
    return SQLITE_OK;
}

static void runtime_shutdown(void *context) {
    /* The shared heap outlives SQLite and its retained backing stays admitted. */
    (void)context;
}

int lp_sqlite_runtime_memory_init(void) {
    const sqlite3_mem_methods methods = {
        runtime_malloc, lp_runtime_c_free, runtime_realloc, runtime_size,
        runtime_roundup, runtime_init, runtime_shutdown, NULL
    };
    /* Startup only, before sqlite3_initialize/open. SQLITE_MISUSE is fatal to
     * managed startup rather than silently retaining the default allocator. */
    return sqlite3_config(SQLITE_CONFIG_MALLOC, &methods);
}

int lp_sqlite_bind_blob_copy(sqlite3_stmt *statement, int index,
                             const void *bytes, int length) {
    return sqlite3_bind_blob(statement, index, bytes, length, SQLITE_TRANSIENT);
}

int lp_sqlite_bind_text_copy(sqlite3_stmt *statement, int index,
                             const char *bytes, int length) {
    return sqlite3_bind_text(statement, index, bytes, length, SQLITE_TRANSIENT);
}
