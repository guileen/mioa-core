/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_GOVERNANCE_H
#define MIOA_GRAPHICS_GOVERNANCE_H

/* Provisional, owner-serialized qualification interface; not a graphics ABI. */
#include "work.h"
#include <stdint.h>
#include <stdbool.h>

#define MG_GOV_DOMAINS 64
#define MG_GOV_READY 128
#define MG_GOV_COMPLETIONS 64
#define MG_GOV_OWNERS 8
#define MG_GOV_PERIOD_NS UINT64_C(100000000)
#define MG_GOV_IO_SLOTS 12
#define MG_GOV_IO_CHUNK (UINT64_C(256) * 1024)
typedef enum { MG_GOV_IO_NETWORK, MG_GOV_IO_DISK, MG_GOV_IO_KINDS } mg_gov_io_kind;
typedef struct {
    uint64_t tokens, remainder;
} mg_gov_io_bucket;
typedef struct {
    uint64_t serial, bytes;
    uint32_t domain, kind;
    bool active;
} mg_gov_io_use;

typedef enum { MG_GOV_FOREGROUND, MG_GOV_VISIBLE, MG_GOV_BACKGROUND,
               MG_GOV_BACKGROUND_AUTHORIZED } mg_gov_class;
typedef struct {
    uint64_t id, deadline_ns, estimate_ns, policy_epoch, cap_epoch;
    bool authorized_kind;
} mg_gov_request;
typedef struct {
    mg_work_domain_ref ref;
    mg_gov_request ready[MG_GOV_READY];
    uint32_t first, count, weight;
    uint64_t vruntime, remainder, quota_ns, policy_epoch, cap_epoch;
    int64_t tokens;
    bool registered, stopping, recently_finished;
    mg_gov_io_bucket io[MG_GOV_IO_KINDS];
    uint64_t io_clock_ns;
    uint32_t io_running;
} mg_gov_domain;
typedef struct {
    bool used;
    uint32_t domain, samples;
    uint64_t id, estimate_ns, started_ns, last_sample_ns, cpu_start_ns, last_cpu_ns;
    uint32_t weight;
    mg_work_ticket ticket;
} mg_gov_running;
typedef struct {
    uint64_t epoch, next_sequence;
    uint64_t active[MG_GOV_COMPLETIONS];
} mg_gov_dispatch_owner;
typedef struct {
    mg_work ledger;
    mg_work_domain ledger_domains[MG_GOV_DOMAINS + 1];
    mg_work_slot ledger_slots[MG_GOV_COMPLETIONS + 1];
    mg_gov_domain domains[MG_GOV_DOMAINS];
    mg_gov_running running[MG_GOV_COMPLETIONS];
    mg_gov_dispatch_owner owners[MG_GOV_OWNERS];
    uint64_t now_ns, period_start_ns, pool_quota_ns;
    int64_t pool_tokens;
    uint32_t cursor, running_limit;
    mg_gov_io_bucket io[MG_GOV_IO_KINDS];
    mg_gov_io_use io_uses[MG_GOV_IO_SLOTS];
    uint64_t io_clock_ns, next_io, io_bytes[MG_GOV_IO_KINDS];
    uint32_t io_running;
    bool io_counter_overflow;
} mg_governance;

typedef void (*mg_gov_stop_fn)(void *context, uint32_t domain);
typedef bool (*mg_gov_cpu_fn)(void *context, uint32_t domain, uint64_t *cpu_ns);
mg_work_result mg_gov_init(mg_governance *, uint32_t concurrency, uint64_t now_ns,
                           mg_lease_key control);
mg_work_result mg_gov_register(mg_governance *, uint32_t domain, mg_lease_key,
                               uint64_t quota_ns, mg_gov_class, uint64_t policy_epoch,
                               uint64_t cap_epoch);
/* Effective at now_ns; settle old-class I/O credit before changing rates.
 * Epoch-only updates retain fractional byte credit. */
mg_work_result mg_gov_policy(mg_governance *, uint32_t domain, mg_gov_class,
                             uint64_t policy_epoch, uint64_t cap_epoch, uint64_t now_ns);
mg_work_result mg_gov_submit(mg_governance *, uint32_t domain, mg_gov_request);
/* Expired/stale queued requests return with slot UINT32_MAX for reconciliation.
 * The trusted CPU callback samples the selected domain before dispatch. */
mg_work_result mg_gov_next(mg_governance *, uint64_t now_ns, mg_gov_cpu_fn, void *context,
                           uint32_t *domain, uint32_t *slot, mg_gov_request *request);
mg_work_result mg_gov_finish(mg_governance *, uint32_t slot, uint64_t now_ns,
                             uint64_t measured_cpu_ns);
mg_work_result mg_gov_stop(mg_governance *, uint32_t domain);
/* Owner must first prove exact child exit. Rearm rotates only work identity:
 * CPU debt, tokens, policy epochs and unrelated accounting remain intact. */
mg_work_result mg_gov_rearm(mg_governance *, uint32_t domain, mg_work_domain_ref *next);
/* Call from an independent trusted monitor, at 100 ms intervals, with cumulative
 * process/thread CPU, not application-reported values. Stop never refunds a slot. */
mg_work_result mg_gov_sample(mg_governance *, uint32_t slot, uint64_t now_ns,
                             uint64_t cumulative_cpu_ns, mg_gov_stop_fn, void *);
/* Each owner serializes authorization and fence in its own event sequence.
 * The returned watermark and active set expose previously authorized work. */
mg_work_result mg_gov_authorize(mg_gov_dispatch_owner *, uint64_t epoch, uint64_t *sequence);
mg_work_result mg_gov_fence(mg_gov_dispatch_owner *, uint64_t epoch, uint64_t *watermark);
mg_work_result mg_gov_settle(mg_gov_dispatch_owner *, uint64_t sequence);
/* Trusted, single-owner I/O admission. Counts native payload bytes, not kernel
 * overhead or physical bandwidth. The executing broker retains its own G6
 * buffer charge until real disposition; these windows are not a memory ledger.
 * Reserve one <=256KiB chunk before dispatch. Rejection has no I/O side effects.
 * Completion requires trusted actual terminal proof; unknown keeps its slot.
 * Stop forbids begin, not settlement. Rearm waits for every old use. */
mg_work_result mg_gov_io_refresh(mg_governance *, uint32_t domain, uint64_t now_ns);
mg_work_result mg_gov_io_begin(mg_governance *, uint32_t domain, mg_gov_io_kind,
                              uint64_t maximum_bytes, uint64_t now_ns, uint64_t *ticket);
mg_work_result mg_gov_io_finish(mg_governance *, uint32_t domain, uint64_t ticket,
                               uint64_t actual_bytes, uint64_t now_ns);
#endif
