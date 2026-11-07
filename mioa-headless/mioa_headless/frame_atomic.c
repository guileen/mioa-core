/* LPFR v2 cross-process atomics; all shared atomic addresses are naturally aligned. */
#include <stdint.h>
#include <stdatomic.h>

int lpfr_supported(void *base) {
    return atomic_is_lock_free((_Atomic uint32_t *)((char *)base + 8)) &&
           atomic_is_lock_free((_Atomic uint64_t *)((char *)base + 16));
}
void lpfr_initialize(void *base) {
    atomic_init((_Atomic uint32_t *)((char *)base + 8), 1);
    atomic_init((_Atomic uint64_t *)((char *)base + 16), 0);
}
uint32_t lpfr_publish(void *base, uint32_t back, uint64_t seq) {
    uint32_t old = atomic_exchange_explicit((_Atomic uint32_t *)((char *)base + 8),
                                             back | 0x100, memory_order_acq_rel);
    atomic_store_explicit((_Atomic uint64_t *)((char *)base + 16), seq, memory_order_release);
    return old & 3;
}
/* Used by the concurrency regression, not by the producer. */
uint32_t lpfr_acquire(void *base, uint32_t front) {
    _Atomic uint32_t *middle = (_Atomic uint32_t *)((char *)base + 8);
    if (!(atomic_load_explicit(middle, memory_order_acquire) & 0x100)) return front;
    return atomic_exchange_explicit(middle, front, memory_order_acq_rel) & 3;
}
