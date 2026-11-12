/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_MEMORY_H
#define MIOA_GRAPHICS_MEMORY_H

#include "lease.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Independent units: host addressable bytes, handle count, device allocation
 * bytes and spool disk bytes. The owner chooses what allocations belong to each
 * budget; no RSS, filesystem quota or OS enforcement is implied. */
typedef enum {
    MG_MEMORY_HOST_BYTES,
    MG_MEMORY_HANDLES,
    MG_MEMORY_DEVICE_BYTES,
    MG_MEMORY_DISK_BYTES,
    MG_MEMORY_DIMENSIONS
} mg_memory_dimension;

typedef struct {
    uint64_t value[MG_MEMORY_DIMENSIONS];
} mg_memory_amount;

typedef enum {
    MG_MEMORY_ORDINARY,
    MG_MEMORY_SYSTEM
} mg_memory_class;

typedef enum {
    MG_MEMORY_OK,
    MG_MEMORY_INVALID,
    MG_MEMORY_STALE,
    MG_MEMORY_BUSY,
    MG_MEMORY_LIMIT,
    MG_MEMORY_FULL,
    MG_MEMORY_EXHAUSTED
} mg_memory_result;

typedef enum {
    MG_MEMORY_FREE,
    MG_MEMORY_RESERVED,
    MG_MEMORY_COMMITTED,
    MG_MEMORY_RETIRING
} mg_memory_state;

typedef struct {
    mg_lease_key account, resource;
    uint64_t serial;
    uint32_t slot;
} mg_memory_ticket;

/* Caller-owned, fixed storage; fields are private after initialization. */
typedef struct {
    mg_lease_key resource;
    mg_memory_amount amount;
    uint64_t serial;
    uint32_t next;
    mg_memory_state state;
    mg_memory_class request_class;
    mg_memory_ticket parent_ticket;
} mg_memory_slot;

typedef struct {
    mg_memory_amount reserved, committed, retiring, ordinary;
    mg_memory_amount occupied, deficit, ordinary_deficit;
    uint32_t ordinary_tickets;
} mg_memory_usage;

typedef struct mg_memory {
    mg_lease_key key;
    mg_memory_slot *slots;
    struct mg_memory *parent;
    mg_lease_key parent_key;
    mg_memory_amount capacity, protected_capacity;
    mg_memory_amount reserved, committed, retiring, ordinary;
    uint64_t next_serial;
    uint32_t slot_count, protected_slots, ordinary_tickets, free_slot;
} mg_memory;

/* Initialize once; never reset a live account or reuse its key. All key fields
 * and slot_count must be nonzero. Storage remains alive and unmoved.
 * protected_slots must be <= slot_count; it reserves metadata headroom for
 * SYSTEM independently of resource-unit headroom. ORDINARY may hold at most
 * slot_count - protected_slots tickets, including zero-amount tickets and all
 * RESERVED/COMMITTED/RETIRING states. SYSTEM still shares the total slot bound.
 * Serialize ALL access externally. Failed operations preserve state and outputs.
 * Inputs/outputs must not alias private account or slot storage.
 * Initialization is O(slot_count); subsequent operations are O(DIMENSIONS)
 * (twice for a shared-parent child), without allocation, callbacks, locks or OS calls.
 * Keys identify, not authorize: trusted owners control all mutations, especially
 * SYSTEM admission and release. Resource keys follow lease.h's identity rules.
 * A resource may have multiple independent tickets; its owner tracks them all.
 * Fixed child grants hold a parent ticket until actual relinquishment.
 * Shared-cap children use init_child instead: every leaf ticket also holds one
 * parent ticket, so independent hard caps compete for actual parent capacity. */
mg_memory_result mg_memory_init(mg_memory *memory, mg_memory_slot *slots,
                                uint32_t slot_count, uint32_t protected_slots,
                                mg_lease_key key,
                                mg_memory_amount capacity,
                                mg_memory_amount protected_capacity);
/* Parent must be a stable initialized standalone account (no parent of its own)
 * serialized with the child. Its key/storage must outlive every child ticket.
 * Each reserve/commit/cancel/retire/release mirrors the same transition into
 * parent, including zero amounts and retained retirement. No capacity is
 * reserved at initialization; child capacity is a cap, not a guarantee. */
mg_memory_result mg_memory_init_child(mg_memory *, mg_memory_slot *, uint32_t slots,
    uint32_t protected_slots, mg_lease_key key, mg_memory_amount capacity,
    mg_memory_amount protection, mg_memory *parent);
/* Protected capacity must be <= total capacity in each dimension. Reductions
 * below occupancy are allowed; existing charges remain and deficits are exposed.
 * All grants freeze while any total deficit exists. ORDINARY also freezes on
 * any ordinary deficit; SYSTEM remains subject to total capacity, not headroom.
 * Headroom limits ordinary occupancy to capacity - protected_capacity; system
 * occupancy is separately constrained by total occupancy <= capacity. */
mg_memory_result mg_memory_set_capacity(mg_memory *memory, mg_lease_key key,
                                        mg_memory_amount capacity,
                                        mg_memory_amount protected_capacity);
/* Reserve ALL dimensions before the real allocation. Zero amounts still occupy
 * a ticket slot. On allocation failure cancel; on success commit actual charge.
 * FULL also reports exhaustion of the ordinary ticket share; only cancellation
 * or trusted release returns a ticket slot, not commit or retirement.
 * A ticket binds account identity, resource identity, serial, and slot. */
mg_memory_result mg_memory_reserve(mg_memory *memory, mg_lease_key key,
                                   mg_lease_key resource,
                                   mg_memory_amount amount,
                                   mg_memory_class request_class,
                                   mg_memory_ticket *ticket);
/* Exactly once, RESERVED -> COMMITTED; actual <= reservation in every unit.
 * Only the unused portion is refunded, including when capacity was reduced. */
mg_memory_result mg_memory_commit(mg_memory *memory, mg_memory_ticket ticket,
                                  mg_memory_amount actual);
/* Only an uncommitted reservation may be canceled. */
mg_memory_result mg_memory_cancel(mg_memory *memory, mg_memory_ticket ticket);
/* COMMITTED -> RETIRING, holding the entire charge. Duplicate retirement is OK. */
mg_memory_result mg_memory_retire(mg_memory *memory, mg_memory_ticket ticket);
/* Trusted owner calls only AFTER actual release, e.g. successful munmap after
 * lease completion. Retirement, timeouts, device loss or take_release alone do
 * not prove disposal. Pure accounting cannot verify caller-provided proof.
 * Freed/stale tickets never refund twice, even after slot reuse. */
mg_memory_result mg_memory_release(mg_memory *memory, mg_memory_ticket ticket);
mg_memory_result mg_memory_get_usage(const mg_memory *memory,
                                     mg_memory_usage *usage);

#ifdef __cplusplus
}
#endif
#endif
