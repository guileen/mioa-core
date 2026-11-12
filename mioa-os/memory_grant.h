/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_MEMORY_GRANT_H
#define MIOA_GRAPHICS_MEMORY_GRANT_H
#include "memory.h"
#include <stddef.h>
#include <stdint.h>

#define MG_MEMORY_GRANT_EXTRA 128
#define MG_MEMORY_GRANT_HEAP_CHUNK (UINT64_C(4) * 1024 * 1024)
/* One trusted launcher-bound connected endpoint, externally serialized with
 * the existing parent mg_memory owner. The child never supplies account,
 * resource, payer, or authorization identity. No public graphics ABI. */
typedef struct {
    mg_memory *memory;
    mg_lease_key account, resource, domain;
    mg_memory_ticket ticket;
    mg_memory_amount pre_reserved;
    uint64_t last_request, launch_id;
    int fd;
    unsigned char request[40], reply[32];
    size_t input, output;
    mg_memory_ticket extra[MG_MEMORY_GRANT_EXTRA];
    unsigned char extra_kind[MG_MEMORY_GRANT_EXTRA]; /* 1 anonymous spool, 2 heap growth */
    bool extra_closed[MG_MEMORY_GRANT_EXTRA];
    uint64_t granted_host_bytes, heap_ceiling;
    bool reply_pending, has_ticket, has_reservation, closure_asserted, exit_observed, closed;
    bool trusted_heap_release, wire_started;
} mg_memory_grant;
/* fd is borrowed, but must be the launcher's private child endpoint; caller
 * binds this record to exact owned domain generation and sole parent ledger.
 * No authorization is inferred from message content or caller-supplied keys. */
mg_memory_result mg_memory_grant_init(mg_memory_grant *, mg_memory *, int fd,
                                      mg_lease_key account, mg_lease_key resource,
                                      mg_lease_key domain, uint64_t launch_id);
/* These three configuration APIs freeze when pump receives its first byte,
 * including partial/malformed records. Before giving the gated child its exec
 * token, reserve its baseline from the assigned child account. Commit cannot
 * exceed this reservation; growth competes with native/GPU backing there. */
mg_memory_result mg_memory_grant_pre_reserve(mg_memory_grant *, uint64_t host_bytes, uint64_t handles);
/* Trusted prelaunch authority only. A nonzero ceiling opts into the native
 * 4MiB baseline/chunk protocol; this informational maximum reserves no quota
 * and must never be below an existing baseline reservation. */
mg_memory_result mg_memory_grant_set_heap_ceiling(mg_memory_grant *, uint64_t host_bytes);
/* Trusted native-core disposal authority only; default is exit-only.
 * Requires a configured native heap ceiling. The launcher attests that op6
 * is sent only after physical disposal, not allocator retirement/free-list use.
 * This is not containment of a hostile native process. */
mg_memory_result mg_memory_grant_allow_trusted_heap_release(mg_memory_grant *);
/* Trusted launch authority only, after proving the child was never released. */
mg_memory_result mg_memory_grant_cancel_prelaunch(mg_memory_grant *);
/* At most one bounded read/write syscall per call. BUSY means retry later;
 * endpoint EOF/errors do not release any charge. MGR1 uses fixed 40/32-byte
 * records. Op3 admits nontransferable unlinked disk spools before creation;
 * op4 admits additional heap bytes below the trusted heap ceiling; op5 with
 * zero grant/bytes/handles returns that ceiling without admitting any bytes.
 * Op2 spool release requires actual disposal. Base heap closure covers all
 * remaining growth tokens, which still require exact process exit for refund.
 * With prelaunch opt-in, op6 supplies the original baseline nonce, a positive
 * multiple of 4MiB and zero handles. It returns only whole live 4MiB growth
 * tickets after full batch prevalidation, never the baseline. Success returns
 * the baseline nonce at reply offset16; offset24 remains reserved zero.
 * Closure, exit, stale/replayed requests and over-return cannot refund.
 * EOF/uncertain ACK never restores client credit or supplies closure.
 * The connected trusted service supplies physical disposal facts. */
mg_memory_result mg_memory_grant_pump(mg_memory_grant *);
/* Trusted wait4/qualified CPU-exit observer only. Baseline and remaining heap
 * release additionally require authenticated closure; EOF alone is no proof. */
mg_memory_result mg_memory_grant_process_exited(mg_memory_grant *, mg_lease_key domain,
                                                uint64_t launch_id);
#endif
