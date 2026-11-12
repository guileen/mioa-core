/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_WORK_H
#define MIOA_GRAPHICS_WORK_H

#include "lease.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    MG_WORK_OK,
    MG_WORK_INVALID,
    MG_WORK_STALE,
    MG_WORK_CLOSED,
    MG_WORK_FULL,
    MG_WORK_BUSY,
    MG_WORK_BUDGET,
    MG_WORK_EXPIRED,
    MG_WORK_EXHAUSTED,
    /* These two finish results consume the completion, unlike failures. */
    MG_WORK_OVERRUN,
    MG_WORK_OVERFLOW
} mg_work_result;

typedef struct {
    uint32_t queued, running;
    uint64_t cpu_ns;
} mg_work_limits;

/* Direct domain handle: key is identity, slot is only a checked lookup hint.
 * Construct from the slot/key accepted by init (slot zero) or register. */
typedef struct {
    mg_lease_key key;
    uint32_t slot;
} mg_work_domain_ref;

typedef struct {
    mg_lease_key domain;
    uint64_t serial;
    uint32_t slot;
} mg_work_ticket;

typedef enum { MG_WORK_NONE, MG_WORK_START, MG_WORK_EXPIRE } mg_work_event_kind;
typedef struct {
    mg_work_event_kind kind;
    mg_work_ticket ticket; /* Zero for NONE; EXPIRE has already been settled. */
} mg_work_event;

typedef enum { MG_WORK_FREE, MG_WORK_QUEUED, MG_WORK_RUNNING } mg_work_state;

/* Caller-owned storage, private after init except for externally serialized reads
 * of domain counters/diagnostics. Do not mutate, copy, move or reinitialize live
 * ledgers. Storage must be disjoint; output pointers must not alias the ledger.
 * Register/rearm identities through a trusted authority: keys aren't capabilities.
 */
typedef struct {
    uint64_t serial, deadline_ns, allowance_ns;
    uint32_t domain_slot, previous, next;
    mg_work_state state;
} mg_work_slot;

typedef struct {
    mg_lease_key key;
    mg_work_limits limits;
    uint64_t consumed_ns, reserved_ns;
    uint32_t queued, running, first, last;
    bool registered, active, blocked, overrun, overflow, shared;
} mg_work_domain;

typedef struct {
    mg_work_domain *domains;
    mg_work_slot *slots;
    uint64_t next_serial, now_ns;
    uint32_t domain_capacity, capacity, running_limit, running;
    uint32_t guaranteed_slots, guaranteed_running, shared_slots, shared_running, free_slot, cursor;
} mg_work;

/* Allocation-free C11 admission/accounting, NOT CPU preemption or a hard quota.
 * Serialize ALL access externally. The trusted caller supplies monotonic ns;
 * time may equal the previous observation, never decrease. CPU measurements are
 * trusted CPU ns, not elapsed wall time (and may exceed wall time for threads).
 * Failures leave state and outputs unchanged, except the documented OVERRUN and
 * OVERFLOW finish outcomes. All capacities, key fields and allowances are > 0.
 * Init once; caller must never reuse an identity in another live/reset ledger.
 * Slot zero is reserved at init for system/control work. Register reserves BOTH
 * queued+running storage and running capacity, with no borrowing/overbooking.
 * Shared domains use only capacity outside all guarantees. A later registration
 * fails if its guarantee would overlap shared work already queued or running.
 * These guarantees concern admission, not OS scheduling or CPU execution time.
 */
mg_work_result mg_work_init(mg_work *work, mg_work_domain *domains,
                           uint32_t domain_capacity, mg_work_slot *slots,
                           uint32_t capacity, uint32_t running_limit,
                           mg_lease_key control, mg_work_limits limits);
/* Private scheduler ledger: slot zero is shared, with no control reservation.
 * The caller supplies independent control/monitor capacity and accounting. */
mg_work_result mg_work_init_shared(mg_work *work, mg_work_domain *domains,
                                  uint32_t domain_capacity, mg_work_slot *slots,
                                  uint32_t capacity, uint32_t running_limit,
                                  mg_lease_key owner, mg_work_limits limits);
/* A domain slot is registered once. Its scope/resource remains bound forever;
 * use rearm for a new generation. This keeps guarantees and CPU debt durable. */
mg_work_result mg_work_register(mg_work *work, uint32_t domain_slot,
                               mg_lease_key key, mg_work_limits limits);
/* Shared admission variant for a higher-level scheduler. It reserves no share
 * and can use only storage and execution capacity outside guaranteed shares. */
mg_work_result mg_work_register_shared(mg_work *work, uint32_t domain_slot,
                                      mg_lease_key key, mg_work_limits limits);
/* Reserve estimated CPU on enqueue. deadline is exclusive: now >= deadline
 * expires queued work. Execution after START is not canceled by the deadline.
 * The domain key supplies the ticket's scope and revocation generation.
 * Enqueue, raise_limit and rearm use constant-time checked domain lookup. */
mg_work_result mg_work_enqueue(mg_work *work, mg_work_domain_ref ref, uint64_t now_ns,
                              uint64_t deadline_ns, uint64_t allowance_ns,
                              mg_work_ticket *ticket);
/* Visits at most domain_capacity heads, FIFO within each domain, round-robin
 * across eligible domains. Returns one START/EXPIRE or NONE. EXPIRE releases
 * its reservation, never starts, and lets the caller settle external ownership.
 * Expiry remains observable even when all running slots/budgets are blocked.
 * NONE is successful and advances the observed clock, but not the cursor. */
mg_work_result mg_work_next(mg_work *work, uint64_t now_ns, mg_work_event *event);
mg_work_result mg_work_cancel(mg_work *work, mg_work_ticket ticket);
/* Stops admission/dispatch; settles all queued tickets and returns their count.
 * Caller must reconcile external ownership for those canceled tickets. Running
 * work remains tracked: revocation, timeout and process exit alone do not prove
 * GPU/I/O completion. This ledger accounts CPU work only. Stop is idempotent. */
mg_work_result mg_work_stop(mg_work *work, mg_work_domain_ref ref, uint32_t *canceled);
/* Trusted completion only after actual CPU completion/proven termination.
 * CPU consumed is never refunded. measured > reserved returns OVERRUN, consumes
 * the ticket, records all measured CPU and blocks future dispatch/admission.
 * Raising the limit explicitly may unblock, but overrun stays visible. If the
 * cumulative sum cannot fit, OVERFLOW consumes the ticket, sets consumed to
 * UINT64_MAX plus a sticky overflow flag, and permanently blocks this domain.
 * Subsequent valid finishes still settle running tickets and return OVERFLOW.
 * Stale/double finishes cannot settle a replacement ticket. */
mg_work_result mg_work_finish(mg_work *work, mg_work_ticket ticket,
                             uint64_t now_ns, uint64_t measured_cpu_ns);
mg_work_result mg_work_raise_limit(mg_work *work, mg_work_domain_ref ref,
                                  uint64_t cpu_ns);
/* Clear an overrun block only when the cumulative limit covers all debt and
 * reservations. Overflow remains permanently blocked. */
mg_work_result mg_work_unblock(mg_work *work, mg_work_domain_ref ref);
/* Only stopped, fully settled domains can rearm. Increments generation without
 * wrap, preserves caps, consumed CPU and diagnostics; never erases CPU debt. */
mg_work_result mg_work_rearm(mg_work *work, mg_work_domain_ref ref, mg_work_domain_ref *next);

#ifdef __cplusplus
}
#endif
#endif
