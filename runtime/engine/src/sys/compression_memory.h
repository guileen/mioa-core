/* Forced into only the pinned compression/HTTP2 heap-owning C sources.
 * Keep upstream callbacks, zeroing, realloc failure ownership and cleanup;
 * replace only their libc backing calls with the physical runtime heap.
 * Assembler names deliberately leave nghttp2_mem's malloc/free/calloc/realloc
 * member names untouched, including caller-supplied allocator dispatch.
 * This is translation-unit-local binding, not global libc interposition.
 * The build disables the four allocation builtins in these sources so the
 * compiler cannot synthesize an unredirected libc allocation. */
#ifndef LP_COMPRESSION_MEMORY_H
#define LP_COMPRESSION_MEMORY_H
#include <stdlib.h>
#define LP_HEAP_STRINGIFY_INNER(value) #value
#define LP_HEAP_STRINGIFY(value) LP_HEAP_STRINGIFY_INNER(value)
#define LP_HEAP_SYMBOL(name) __asm__(LP_HEAP_STRINGIFY(__USER_LABEL_PREFIX__) name)
extern void *malloc(size_t size) LP_HEAP_SYMBOL("lp_runtime_malloc");
extern void *calloc(size_t count, size_t size) LP_HEAP_SYMBOL("lp_runtime_calloc");
extern void *realloc(void *pointer, size_t size) LP_HEAP_SYMBOL("lp_runtime_realloc");
extern void free(void *pointer) LP_HEAP_SYMBOL("lp_runtime_c_free");
#undef LP_HEAP_SYMBOL
#undef LP_HEAP_STRINGIFY
#undef LP_HEAP_STRINGIFY_INNER
#endif
