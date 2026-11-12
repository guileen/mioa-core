/* SPDX-License-Identifier: MIT */
#include "lease.h"

static bool matches(mg_lease_key a, mg_lease_key b) {
    return a.scope == b.scope && a.resource == b.resource &&
           a.generation == b.generation;
}

mg_lease_result mg_lease_init(mg_lease *lease, mg_lease_slot *slots,
                            uint32_t capacity, mg_lease_key key) {
    if (!lease || !slots || !capacity || !key.scope || !key.resource ||
        !key.generation) return MG_LEASE_INVALID;
    for (uint32_t i = 0; i < capacity; ++i) {
        slots[i].serial = 0;
        slots[i].next = i == capacity - 1 ? UINT32_MAX : i + 1;
    }
    *lease = (mg_lease){.key = key, .slots = slots, .next_serial = 1,
        .capacity = capacity, .free_slot = 0, .pending = 0,
        .state = MG_LEASE_OPEN};
    return MG_LEASE_OK;
}

mg_lease_result mg_lease_acquire(mg_lease *lease, mg_lease_key key,
                               mg_lease_use *use) {
    if (!lease || !use) return MG_LEASE_INVALID;
    if (!matches(lease->key, key)) return MG_LEASE_STALE;
    if (lease->state != MG_LEASE_OPEN) return MG_LEASE_CLOSED;
    if (!lease->next_serial) return MG_LEASE_EXHAUSTED;
    if (lease->free_slot == UINT32_MAX) return MG_LEASE_FULL;
    uint32_t index = lease->free_slot;
    mg_lease_slot *slot = &lease->slots[index];
    uint64_t serial = lease->next_serial;
    lease->next_serial = serial == UINT64_MAX ? 0 : serial + 1;
    lease->free_slot = slot->next;
    slot->serial = serial;
    ++lease->pending;
    *use = (mg_lease_use){.lease = key, .serial = serial, .slot = index};
    return MG_LEASE_OK;
}

mg_lease_result mg_lease_complete(mg_lease *lease, mg_lease_use use) {
    if (!lease) return MG_LEASE_INVALID;
    if (!matches(lease->key, use.lease) || use.slot >= lease->capacity ||
        !use.serial) return MG_LEASE_STALE;
    mg_lease_slot *slot = &lease->slots[use.slot];
    if (slot->serial != use.serial) return MG_LEASE_STALE;
    slot->serial = 0;
    slot->next = lease->free_slot;
    lease->free_slot = use.slot;
    --lease->pending;
    return MG_LEASE_OK;
}

mg_lease_result mg_lease_retire(mg_lease *lease, mg_lease_key key) {
    if (!lease) return MG_LEASE_INVALID;
    if (!matches(lease->key, key)) return MG_LEASE_STALE;
    if (lease->state == MG_LEASE_OPEN) lease->state = MG_LEASE_RETIRING;
    return MG_LEASE_OK;
}

bool mg_lease_take_release(mg_lease *lease, mg_lease_key key) {
    if (!lease || !matches(lease->key, key) ||
        lease->state != MG_LEASE_RETIRING || lease->pending) return false;
    lease->state = MG_LEASE_RELEASED;
    return true;
}

mg_lease_result mg_lease_rearm(mg_lease *lease, mg_lease_key key,
                             mg_lease_key *next) {
    if (!lease || !next) return MG_LEASE_INVALID;
    if (!matches(lease->key, key)) return MG_LEASE_STALE;
    if (lease->state != MG_LEASE_RELEASED) return MG_LEASE_BUSY;
    if (lease->key.generation == UINT64_MAX) return MG_LEASE_EXHAUSTED;
    ++lease->key.generation;
    lease->next_serial = 1;
    lease->state = MG_LEASE_OPEN;
    *next = lease->key;
    return MG_LEASE_OK;
}
