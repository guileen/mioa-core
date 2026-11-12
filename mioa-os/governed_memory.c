/* SPDX-License-Identifier: MIT */
#include "governed_memory.h"
#include <stddef.h>
#include <stdatomic.h>

static _Atomic uint64_t next_host_scope = 1;

mg_memory_result mg_governed_memory_init_host(mg_governed_memory *tree, uint32_t app_count) {
    uint64_t scope = atomic_load_explicit(&next_host_scope, memory_order_relaxed);
    for (;;) {
        if (scope == UINT64_MAX) return MG_MEMORY_EXHAUSTED;
        if (atomic_compare_exchange_weak_explicit(&next_host_scope, &scope, scope + 1,
                                                  memory_order_relaxed, memory_order_relaxed))
            break;
    }
    return mg_governed_memory_init_apps(tree, scope, app_count);
}

#define MIB (UINT64_C(1024) * UINT64_C(1024))

enum {
    GOVERNED_NEW,
    GOVERNED_ACTIVE,
    GOVERNED_CLOSED,
    GOVERNED_FAILED
};

static const mg_memory_amount root_capacity = {{1536 * MIB, 1024, 768 * MIB, 512 * MIB}};
static const mg_memory_amount root_protection = {{128 * MIB, 128, 0, 0}};
static const mg_memory_amount child_capacity[MG_G6_COUNT] = {
    {{512 * MIB, 256, 256 * MIB, 128 * MIB}},
    {{512 * MIB, 256, 256 * MIB, 128 * MIB}},
    {{256 * MIB, 256, 256 * MIB, 128 * MIB}},
    {{128 * MIB, 128, 0, 128 * MIB}}
};
static const mg_memory_amount app_protection = {{8 * MIB, 16, 0, 0}};

/* The caller supplies a never-reused incarnation; identifiers are distinct
 * within it. No global allocator, pointer-derived identity, or mutable epoch. */
static mg_lease_key account_key(uint64_t scope, uint32_t identifier) {
    return (mg_lease_key){scope, identifier, 1};
}

static void unwind(mg_governed_memory *tree) {
    for (uint32_t kind = 0; kind < tree->account_count; ++kind) {
        mg_memory_ticket ticket = tree->parent_grants[kind];
        if (!ticket.serial) continue;
        (void)mg_memory_retire(&tree->root, ticket);
        (void)mg_memory_release(&tree->root, ticket);
    }
    if (tree->app_pool_grant.serial) {
        (void)mg_memory_retire(&tree->root, tree->app_pool_grant);
        (void)mg_memory_release(&tree->root, tree->app_pool_grant);
    }
}

static mg_memory_result init_tree(mg_governed_memory *tree, uint64_t scope,
                                  uint32_t app_count, bool shared) {
    if (!tree || !scope || app_count < 2 || app_count > MG_G6_MAX_APPS)
        return MG_MEMORY_INVALID;
    if (tree->state == GOVERNED_ACTIVE) return MG_MEMORY_BUSY;
    if (tree->state != GOVERNED_NEW) return MG_MEMORY_STALE;
    tree->state = GOVERNED_FAILED;
    tree->app_count = app_count;
    tree->account_count = MG_G6_COUNT + app_count - 2;
    mg_memory_result result = mg_memory_init(&tree->root, tree->root_slots,
        MG_G6_ROOT_SLOTS, MG_G6_ROOT_PROTECTED_SLOTS, account_key(scope, 1),
        root_capacity, root_protection);
    if (result != MG_MEMORY_OK) return result;
    if (shared) {
        mg_memory_amount capacity = {{1024 * MIB, 512, 512 * MIB, 256 * MIB}};
        mg_memory_amount protection = {{8 * MIB * app_count, 16 * app_count, 0, 0}};
        mg_lease_key key = account_key(scope, MG_G6_ACCOUNT_COUNT + 2);
        result = mg_memory_init(&tree->app_pool, tree->app_pool_slots,
            MG_G6_MAX_APPS * MG_G6_CHILD_SLOTS, app_count * MG_G6_CHILD_PROTECTED_SLOTS,
            key, capacity, protection);
        if (result != MG_MEMORY_OK) return result;
        result = mg_memory_reserve(&tree->root, tree->root.key, key, capacity,
            MG_MEMORY_ORDINARY, &tree->app_pool_grant);
        if (result != MG_MEMORY_OK) return result;
        result = mg_memory_commit(&tree->root, tree->app_pool_grant, capacity);
        if (result != MG_MEMORY_OK) {
            (void)mg_memory_cancel(&tree->root, tree->app_pool_grant);
            return result;
        }
    }
    for (uint32_t kind = 0; kind < tree->account_count; ++kind) {
        bool app = kind <= MG_G6_APP_B || kind >= MG_G6_COUNT;
        mg_memory_amount capacity = child_capacity[app ? MG_G6_APP_A : kind];
        mg_memory_amount protection = app ? app_protection : (mg_memory_amount){{0}};
        mg_lease_key key = account_key(scope, kind + 2);
        if (shared && app) {
            result = mg_memory_init_child(&tree->children[kind], tree->child_slots[kind],
                MG_G6_CHILD_SLOTS, MG_G6_CHILD_PROTECTED_SLOTS, key,
                capacity, protection, &tree->app_pool);
        } else {
            result = mg_memory_reserve(&tree->root, tree->root.key, key, capacity,
                MG_MEMORY_ORDINARY, &tree->parent_grants[kind]);
            if (result != MG_MEMORY_OK) break;
            result = mg_memory_commit(&tree->root, tree->parent_grants[kind], capacity);
            if (result != MG_MEMORY_OK) {
                (void)mg_memory_cancel(&tree->root, tree->parent_grants[kind]);
                tree->parent_grants[kind] = (mg_memory_ticket){0};
                break;
            }
            result = mg_memory_init(&tree->children[kind], tree->child_slots[kind],
                MG_G6_CHILD_SLOTS, MG_G6_CHILD_PROTECTED_SLOTS, key, capacity, protection);
        }
        if (result != MG_MEMORY_OK) break;
    }
    if (result != MG_MEMORY_OK) {
        unwind(tree);
        return result;
    }
    tree->state = GOVERNED_ACTIVE;
    return MG_MEMORY_OK;
}

mg_memory_result mg_governed_memory_init(mg_governed_memory *tree, uint64_t scope) {
    return init_tree(tree, scope, 2, false);
}

mg_memory_result mg_governed_memory_init_apps(mg_governed_memory *tree, uint64_t scope,
                                             uint32_t app_count) {
    return init_tree(tree, scope, app_count, true);
}

mg_memory *mg_governed_memory_account(mg_governed_memory *tree, uint32_t kind) {
    if (!tree || tree->state != GOVERNED_ACTIVE || kind >= tree->account_count) return NULL;
    return &tree->children[kind];
}

mg_memory *mg_governed_memory_app(mg_governed_memory *tree, uint32_t app_slot) {
    if (!tree || app_slot >= tree->app_count) return NULL;
    uint32_t kind = app_slot < 2 ? app_slot : MG_G6_COUNT + app_slot - 2;
    return mg_governed_memory_account(tree, kind);
}

mg_memory_result mg_governed_memory_close(mg_governed_memory *tree) {
    if (!tree) return MG_MEMORY_INVALID;
    if (tree->state == GOVERNED_CLOSED) return MG_MEMORY_STALE;
    if (tree->state != GOVERNED_ACTIVE) return MG_MEMORY_INVALID;

    /* Amount counters alone cannot see zero-amount or SYSTEM tickets. Complete
     * preflight before retiring even one grant preserves atomic BUSY refusal. */
    for (uint32_t kind = 0; kind < tree->account_count; ++kind)
        for (uint32_t slot = 0; slot < MG_G6_CHILD_SLOTS; ++slot)
            if (tree->child_slots[kind][slot].state != MG_MEMORY_FREE)
                return MG_MEMORY_BUSY;
    if (tree->app_pool_grant.serial)
        for (uint32_t slot = 0; slot < MG_G6_MAX_APPS * MG_G6_CHILD_SLOTS; ++slot)
            if (tree->app_pool_slots[slot].state != MG_MEMORY_FREE)
                return MG_MEMORY_BUSY;

    /* Root is private: only these fixed, committed grants can exist here. */
    for (uint32_t kind = 0; kind < tree->account_count; ++kind) {
        if (!tree->parent_grants[kind].serial) continue;
        mg_memory_result result = mg_memory_retire(&tree->root, tree->parent_grants[kind]);
        if (result != MG_MEMORY_OK) return result;
        result = mg_memory_release(&tree->root, tree->parent_grants[kind]);
        if (result != MG_MEMORY_OK) return result;
    }
    if (tree->app_pool_grant.serial) {
        mg_memory_result result = mg_memory_retire(&tree->root, tree->app_pool_grant);
        if (result != MG_MEMORY_OK) return result;
        result = mg_memory_release(&tree->root, tree->app_pool_grant);
        if (result != MG_MEMORY_OK) return result;
    }
    tree->state = GOVERNED_CLOSED;
    return MG_MEMORY_OK;
}
