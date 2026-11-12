/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_GOVERNED_MEMORY_H
#define MIOA_GRAPHICS_GOVERNED_MEMORY_H

#include "memory.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Private Host policy, not part of resource-v1 or the public graphics ABI. */
typedef enum {
    MG_G6_APP_A = 0,
    MG_G6_APP_B = 1,
    MG_G6_SYSTEM = 2,
    MG_G6_PROFILE = 3,
    MG_G6_COUNT = 4
} mg_governed_memory_kind;
enum { MG_G6_MAX_APPS = 5, MG_G6_ACCOUNT_COUNT = MG_G6_COUNT + MG_G6_MAX_APPS - 2 };

enum {
    MG_G6_ROOT_SLOTS = MG_G6_ACCOUNT_COUNT + 4,
    MG_G6_ROOT_PROTECTED_SLOTS = 2,
    MG_G6_CHILD_SLOTS = 256,
    MG_G6_CHILD_PROTECTED_SLOTS = 16
};

/* Caller-owned fixed storage. Initialize to {0}, initialize ONCE, and keep the
 * entire object alive and unmoved until every bound Resource, readback, grant,
 * and borrowed account pointer has been relinquished. Revoking/recycling an
 * endpoint does not shorten this lifetime. Never copy or reset this object.
 * scope is a trusted, nonzero, unique tree incarnation: never reuse it for
 * another tree, even after close. Account/resource keys derive from scope and
 * distinct nonzero resource identifiers with generation 1.
 *
 * One Host executor serializes ALL access, including inspection and close.
 * The embedded root and parent grants are inspection-only; callers must not
 * mutate them or change child capacities/keys/storage. Child ticket operations
 * use the existing mg_memory API and its lifetime/disposal rules, with no second
 * accounting authority. Root holds the
 * ENTIRE child envelope until successful final close; leaf tickets subdivide
 * that grant and must not also be charged to root.
 *
 * For these accounts HOST_BYTES means unified managed native backing INCLUDING
 * GPU backing. DEVICE_BYTES is its GPU/native subset, NOT additional HOST bytes.
 * CPU backing charges {HOST=N, DEVICE=0}; GPU backing charges {HOST=N, DEVICE=N}.
 * HANDLES and DISK_BYTES remain independent. These are admission-ledger units,
 * not physical RSS, residency, filesystem quotas, or OS-enforced limits.
 *
 * Only trusted recovery/GC/OOM code may request MG_MEMORY_SYSTEM admission.
 * The protected App share is inside its envelope, not an extra grant or a
 * preallocated physical recovery heap. All fields are private except bounded
 * read-only inspection of root/children/slots on the same executor. */
typedef struct {
    mg_memory root;
    mg_memory children[MG_G6_ACCOUNT_COUNT];
    mg_memory_slot root_slots[MG_G6_ROOT_SLOTS];
    mg_memory_slot child_slots[MG_G6_ACCOUNT_COUNT][MG_G6_CHILD_SLOTS];
    mg_memory_ticket parent_grants[MG_G6_ACCOUNT_COUNT];
    mg_memory app_pool;
    mg_memory_slot app_pool_slots[MG_G6_MAX_APPS * MG_G6_CHILD_SLOTS];
    mg_memory_ticket app_pool_grant;
    uint32_t account_count, app_count;
    uint32_t state;
} mg_governed_memory;

/* No allocation, locks, callbacks, or OS calls. Invalid scope preserves storage;
 * active reinitialization returns BUSY, and a closed/failed incarnation cannot
 * be reused (STALE). A partial failure cancels/releases all acquired grants. */
mg_memory_result mg_governed_memory_init(mg_governed_memory *, uint64_t scope);
/* Explicit bounded App capacity. Each App retains its 512 MiB hard cap while
 * actual tickets compete for ONE 1024 MiB pool, not five fixed reservations.
 * Each App has 8 MiB protected recovery; the pool protects their combined share.
 * The pool's single parent grant remains inside the unchanged Root envelope. */
mg_memory_result mg_governed_memory_init_apps(mg_governed_memory *, uint64_t scope,
                                             uint32_t app_count);
/* Process-local Host incarnation allocation; caller still owns stable storage.
 * Exhaustion is permanent and no identity is reused. */
mg_memory_result mg_governed_memory_init_host(mg_governed_memory *, uint32_t app_count);
mg_memory *mg_governed_memory_app(mg_governed_memory *, uint32_t app_slot);
/* Borrowed pointer, stable for the active tree's lifetime. Invalid kind or an
 * inactive tree returns NULL. Never use an earlier borrowed pointer after close. */
mg_memory *mg_governed_memory_account(mg_governed_memory *, uint32_t kind);
/* Final shutdown only AFTER all issuers are quiescent. ANY non-FREE child slot,
 * including a zero-amount SYSTEM ticket, returns BUSY with NO state changes.
 * Otherwise retire/release all parent grants and close. A second close is STALE.
 * Successful close invalidates permission to use every borrowed child pointer. */
mg_memory_result mg_governed_memory_close(mg_governed_memory *);

#ifdef __cplusplus
}
#endif
#endif
