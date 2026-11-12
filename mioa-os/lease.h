/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_LEASE_H
#define MIOA_GRAPHICS_LEASE_H

#include <stdbool.h>
#include "api/system/lease_types.h"

#ifdef __cplusplus
extern "C" {
#endif


typedef struct {
    mg_lease_key lease;
    uint64_t serial;
    uint32_t slot;
} mg_lease_use;

typedef enum {
    MG_LEASE_OK,
    MG_LEASE_INVALID,
    MG_LEASE_STALE,
    MG_LEASE_CLOSED,
    MG_LEASE_FULL,
    MG_LEASE_BUSY,
    MG_LEASE_EXHAUSTED
} mg_lease_result;

typedef enum {
    MG_LEASE_OPEN,
    MG_LEASE_RETIRING,
    MG_LEASE_RELEASED
} mg_lease_state;

/* Caller-owned storage; fields are private to lease.c after initialization. */
typedef struct {
    uint64_t serial;
    uint32_t next;
} mg_lease_slot;

typedef struct {
    mg_lease_key key;
    mg_lease_slot *slots;
    uint64_t next_serial;
    uint32_t capacity, free_slot, pending;
    mg_lease_state state;
} mg_lease;

/* Initialize once, never reset a live ledger. All key fields and capacity are
 * nonzero. The authority must never reuse (scope, resource, generation).
 * Storage and ledger must remain alive and unmoved while operations use them.
 * Serialize ALL access externally; this is not a concurrent or shared-memory ABI.
 * Failed calls leave state and output parameters unchanged. */
mg_lease_result mg_lease_init(mg_lease *lease, mg_lease_slot *slots,
                            uint32_t capacity, mg_lease_key key);
/* Acquire before publishing any CPU, queued, I/O or GPU use to another actor. */
mg_lease_result mg_lease_acquire(mg_lease *lease, mg_lease_key key,
                               mg_lease_use *use);
/* Trusted owner calls only after actual completion or proven safe cancellation.
 * A timeout, disconnect, View closure or device generation change is NOT proof.
 * Duplicates/stale tickets return STALE and never decrement another use. */
mg_lease_result mg_lease_complete(mg_lease *lease, mg_lease_use use);
/* Stops new uses; idempotent for this key, including after release. */
mg_lease_result mg_lease_retire(mg_lease *lease, mg_lease_key key);
/* Grants disposal permission once, only when retired and every use completed.
 * Does not free memory, invoke callbacks, or prove backend completion. */
bool mg_lease_take_release(mg_lease *lease, mg_lease_key key);
/* After disposal, reuse the ledger for the same resource in a new generation.
 * No allocation or slot scan. Refuses live leases and generation wraparound. */
mg_lease_result mg_lease_rearm(mg_lease *lease, mg_lease_key key,
                             mg_lease_key *next);

#ifdef __cplusplus
}
#endif
#endif
