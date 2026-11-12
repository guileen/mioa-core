/* Included only in pinned BoringSSL's heap-owning translation units.
 * Mach-O has no usable OPENSSL_memory_* weak hooks in this revision. Keep
 * BoringSSL's size prefix, cleanse and realloc; also cover its raw error-state,
 * pthread-local pointer-array and SSL record-buffer allocations. This is not
 * process libc interposition. */
#ifndef LP_BORINGSSL_MEMORY_H
#define LP_BORINGSSL_MEMORY_H
#include <stdlib.h>
#ifdef __cplusplus
extern "C" {
#endif
void *lp_runtime_malloc(size_t size);
void lp_runtime_c_free(void *pointer);
#ifdef __cplusplus
}
#endif
#define malloc lp_runtime_malloc
#define free lp_runtime_c_free
#endif
