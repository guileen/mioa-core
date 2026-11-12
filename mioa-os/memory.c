/* SPDX-License-Identifier: MIT */
#include "memory.h"
#include <stddef.h>

static bool matches(mg_lease_key a, mg_lease_key b) {
    return a.scope == b.scope && a.resource == b.resource &&
           a.generation == b.generation;
}

static bool valid_key(mg_lease_key key) {
    return key.scope && key.resource && key.generation;
}

static bool valid_capacity(mg_memory_amount capacity,
                           mg_memory_amount protected_capacity) {
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d)
        if (protected_capacity.value[d] > capacity.value[d]) return false;
    return true;
}

/* Every transition preserves occupied <= UINT64_MAX, even after a cap cut. */
static uint64_t occupied(const mg_memory *memory, unsigned d) {
    return memory->reserved.value[d] + memory->committed.value[d] +
           memory->retiring.value[d];
}

static mg_memory_slot *resolve(mg_memory *memory, mg_memory_ticket ticket) {
    if (!matches(memory->key, ticket.account) || !ticket.serial ||
        ticket.slot >= memory->slot_count) return NULL;
    mg_memory_slot *slot = &memory->slots[ticket.slot];
    if (slot->serial != ticket.serial || slot->state == MG_MEMORY_FREE ||
        !matches(slot->resource, ticket.resource)) return NULL;
    return slot;
}

static void free_slot(mg_memory *memory, mg_memory_ticket ticket,
                      mg_memory_slot *slot) {
    if (slot->request_class == MG_MEMORY_ORDINARY) --memory->ordinary_tickets;
    slot->serial = 0;
    slot->state = MG_MEMORY_FREE;
    slot->next = memory->free_slot;
    memory->free_slot = ticket.slot;
}

mg_memory_result mg_memory_init(mg_memory *memory, mg_memory_slot *slots,
                                uint32_t slot_count, uint32_t protected_slots,
                                mg_lease_key key,
                                mg_memory_amount capacity,
                                mg_memory_amount protected_capacity) {
    if (!memory || !slots || !slot_count || protected_slots > slot_count ||
        !valid_key(key) || !valid_capacity(capacity, protected_capacity))
        return MG_MEMORY_INVALID;
    for (uint32_t i = 0; i < slot_count; ++i) {
        slots[i] = (mg_memory_slot){.state = MG_MEMORY_FREE,
            .next = i == slot_count - 1 ? UINT32_MAX : i + 1};
    }
    *memory = (mg_memory){.key = key, .slots = slots, .capacity = capacity,
        .protected_capacity = protected_capacity, .next_serial = 1,
        .slot_count = slot_count, .protected_slots = protected_slots, .free_slot = 0};
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_init_child(mg_memory *memory, mg_memory_slot *slots,
    uint32_t slot_count, uint32_t protected_slots, mg_lease_key key,
    mg_memory_amount capacity, mg_memory_amount protection, mg_memory *parent) {
    if (!parent || parent == memory || parent->parent || !parent->slots ||
        !valid_key(parent->key)) return MG_MEMORY_INVALID;
    mg_memory_result result = mg_memory_init(memory, slots, slot_count,
        protected_slots, key, capacity, protection);
    if (result == MG_MEMORY_OK) {
        memory->parent = parent;
        memory->parent_key = parent->key;
    }
    return result;
}

mg_memory_result mg_memory_set_capacity(mg_memory *memory, mg_lease_key key,
                                        mg_memory_amount capacity,
                                        mg_memory_amount protected_capacity) {
    if (!memory || !valid_capacity(capacity, protected_capacity))
        return MG_MEMORY_INVALID;
    if (!matches(memory->key, key)) return MG_MEMORY_STALE;
    memory->capacity = capacity;
    memory->protected_capacity = protected_capacity;
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_reserve(mg_memory *memory, mg_lease_key key,
                                   mg_lease_key resource,
                                   mg_memory_amount amount,
                                   mg_memory_class request_class,
                                   mg_memory_ticket *ticket) {
    if (!memory || !ticket || !valid_key(resource) ||
        (request_class != MG_MEMORY_ORDINARY && request_class != MG_MEMORY_SYSTEM))
        return MG_MEMORY_INVALID;
    if (!matches(memory->key, key)) return MG_MEMORY_STALE;
    if (!memory->next_serial) return MG_MEMORY_EXHAUSTED;
    if (memory->free_slot == UINT32_MAX) return MG_MEMORY_FULL;
    if (request_class == MG_MEMORY_ORDINARY &&
        memory->ordinary_tickets >= memory->slot_count - memory->protected_slots)
        return MG_MEMORY_FULL;
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        uint64_t used = occupied(memory, d);
        uint64_t limit = memory->capacity.value[d];
        /* Subtraction, never speculative addition, checks both deficit and
         * UINT64_MAX boundary before any dimension or slot is mutated. */
        if (used > limit || amount.value[d] > limit - used) return MG_MEMORY_LIMIT;
        if (request_class == MG_MEMORY_ORDINARY) {
            limit -= memory->protected_capacity.value[d];
            used = memory->ordinary.value[d];
            if (used > limit || amount.value[d] > limit - used)
                return MG_MEMORY_LIMIT;
        }
    }
    mg_memory_ticket parent_ticket = {0};
    if (memory->parent) {
        mg_memory_result result = mg_memory_reserve(memory->parent,
            memory->parent_key, resource, amount, request_class, &parent_ticket);
        if (result != MG_MEMORY_OK) return result;
    }
    uint32_t index = memory->free_slot;
    mg_memory_slot *slot = &memory->slots[index];
    uint64_t serial = memory->next_serial;
    memory->next_serial = serial == UINT64_MAX ? 0 : serial + 1;
    memory->free_slot = slot->next;
    *slot = (mg_memory_slot){.resource = resource, .amount = amount,
        .serial = serial, .next = UINT32_MAX, .state = MG_MEMORY_RESERVED,
        .request_class = request_class, .parent_ticket = parent_ticket};
    if (request_class == MG_MEMORY_ORDINARY) ++memory->ordinary_tickets;
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        memory->reserved.value[d] += amount.value[d];
        if (request_class == MG_MEMORY_ORDINARY)
            memory->ordinary.value[d] += amount.value[d];
    }
    *ticket = (mg_memory_ticket){.account = key, .resource = resource,
        .serial = serial, .slot = index};
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_commit(mg_memory *memory, mg_memory_ticket ticket,
                                  mg_memory_amount actual) {
    if (!memory) return MG_MEMORY_INVALID;
    mg_memory_slot *slot = resolve(memory, ticket);
    if (!slot) return MG_MEMORY_STALE;
    if (slot->state != MG_MEMORY_RESERVED) return MG_MEMORY_BUSY;
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d)
        if (actual.value[d] > slot->amount.value[d]) return MG_MEMORY_INVALID;
    if (memory->parent) {
        mg_memory_result result = mg_memory_commit(memory->parent, slot->parent_ticket, actual);
        if (result != MG_MEMORY_OK) return result;
    }
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        memory->reserved.value[d] -= slot->amount.value[d];
        memory->committed.value[d] += actual.value[d];
        if (slot->request_class == MG_MEMORY_ORDINARY)
            memory->ordinary.value[d] -= slot->amount.value[d] - actual.value[d];
    }
    slot->amount = actual;
    slot->state = MG_MEMORY_COMMITTED;
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_cancel(mg_memory *memory, mg_memory_ticket ticket) {
    if (!memory) return MG_MEMORY_INVALID;
    mg_memory_slot *slot = resolve(memory, ticket);
    if (!slot) return MG_MEMORY_STALE;
    if (slot->state != MG_MEMORY_RESERVED) return MG_MEMORY_BUSY;
    if (memory->parent) {
        mg_memory_result result = mg_memory_cancel(memory->parent, slot->parent_ticket);
        if (result != MG_MEMORY_OK) return result;
    }
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        memory->reserved.value[d] -= slot->amount.value[d];
        if (slot->request_class == MG_MEMORY_ORDINARY)
            memory->ordinary.value[d] -= slot->amount.value[d];
    }
    free_slot(memory, ticket, slot);
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_retire(mg_memory *memory, mg_memory_ticket ticket) {
    if (!memory) return MG_MEMORY_INVALID;
    mg_memory_slot *slot = resolve(memory, ticket);
    if (!slot) return MG_MEMORY_STALE;
    if (slot->state == MG_MEMORY_RETIRING) return MG_MEMORY_OK;
    if (slot->state != MG_MEMORY_COMMITTED) return MG_MEMORY_BUSY;
    if (memory->parent) {
        mg_memory_result result = mg_memory_retire(memory->parent, slot->parent_ticket);
        if (result != MG_MEMORY_OK) return result;
    }
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        memory->committed.value[d] -= slot->amount.value[d];
        memory->retiring.value[d] += slot->amount.value[d];
    }
    slot->state = MG_MEMORY_RETIRING;
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_release(mg_memory *memory, mg_memory_ticket ticket) {
    if (!memory) return MG_MEMORY_INVALID;
    mg_memory_slot *slot = resolve(memory, ticket);
    if (!slot) return MG_MEMORY_STALE;
    if (slot->state != MG_MEMORY_RETIRING) return MG_MEMORY_BUSY;
    if (memory->parent) {
        mg_memory_result result = mg_memory_release(memory->parent, slot->parent_ticket);
        if (result != MG_MEMORY_OK) return result;
    }
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        memory->retiring.value[d] -= slot->amount.value[d];
        if (slot->request_class == MG_MEMORY_ORDINARY)
            memory->ordinary.value[d] -= slot->amount.value[d];
    }
    free_slot(memory, ticket, slot);
    return MG_MEMORY_OK;
}

mg_memory_result mg_memory_get_usage(const mg_memory *memory,
                                     mg_memory_usage *usage) {
    if (!memory || !usage) return MG_MEMORY_INVALID;
    mg_memory_usage value = {.reserved = memory->reserved,
        .committed = memory->committed, .retiring = memory->retiring,
        .ordinary = memory->ordinary, .ordinary_tickets = memory->ordinary_tickets};
    for (unsigned d = 0; d < MG_MEMORY_DIMENSIONS; ++d) {
        uint64_t used = occupied(memory, d);
        uint64_t limit = memory->capacity.value[d];
        value.occupied.value[d] = used;
        value.deficit.value[d] = used > limit ? used - limit : 0;
        limit -= memory->protected_capacity.value[d];
        used = memory->ordinary.value[d];
        value.ordinary_deficit.value[d] = used > limit ? used - limit : 0;
    }
    *usage = value;
    return MG_MEMORY_OK;
}
