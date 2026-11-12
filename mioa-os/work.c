/* SPDX-License-Identifier: MIT */
#include "work.h"

#define NO_SLOT UINT32_MAX

static bool same_key(mg_lease_key a, mg_lease_key b) {
    return a.scope == b.scope && a.resource == b.resource &&
           a.generation == b.generation;
}

static bool valid_key(mg_lease_key key) {
    return key.scope && key.resource && key.generation;
}

static bool fits(mg_work_limits limits, uint32_t slots, uint32_t running) {
    return limits.queued && limits.running && limits.cpu_ns &&
           limits.running <= running && limits.running <= slots &&
           limits.queued <= slots - limits.running;
}

static mg_work_domain *domain_for(mg_work *work, mg_work_domain_ref ref) {
    if (ref.slot >= work->domain_capacity) return 0;
    mg_work_domain *domain = &work->domains[ref.slot];
    return domain->registered && same_key(domain->key, ref.key) ? domain : 0;
}

static mg_work_slot *slot_for(mg_work *work, mg_work_ticket ticket) {
    if (!ticket.serial || ticket.slot >= work->capacity) return 0;
    mg_work_slot *slot = &work->slots[ticket.slot];
    if (slot->state == MG_WORK_FREE || slot->serial != ticket.serial ||
        !same_key(work->domains[slot->domain_slot].key, ticket.domain)) return 0;
    return slot;
}

static void register_domain(mg_work *work, uint32_t index, mg_lease_key key,
                            mg_work_limits limits, bool reserve) {
    work->domains[index] = (mg_work_domain){.key = key, .limits = limits,
        .first = NO_SLOT, .last = NO_SLOT, .registered = true, .active = true, .shared = !reserve};
    if (reserve) {
        work->guaranteed_slots += limits.queued + limits.running;
        work->guaranteed_running += limits.running;
    }
}

static mg_work_result init(mg_work *work, mg_work_domain *domains,
                           uint32_t domain_capacity, mg_work_slot *slots,
                           uint32_t capacity, uint32_t running_limit,
                           mg_lease_key control, mg_work_limits limits, bool reserve) {
    if (!work || !domains || !slots || !domain_capacity || !capacity ||
        !running_limit || running_limit > capacity || !valid_key(control) ||
        !fits(limits, capacity, running_limit)) return MG_WORK_INVALID;
    for (uint32_t i = 0; i < domain_capacity; ++i)
        domains[i] = (mg_work_domain){0};
    for (uint32_t i = 0; i < capacity; ++i)
        slots[i] = (mg_work_slot){.state = MG_WORK_FREE,
            .next = i == capacity - 1 ? NO_SLOT : i + 1};
    *work = (mg_work){.domains = domains, .slots = slots, .next_serial = 1,
        .domain_capacity = domain_capacity, .capacity = capacity,
        .running_limit = running_limit, .free_slot = 0};
    register_domain(work, 0, control, limits, reserve);
    return MG_WORK_OK;
}

mg_work_result mg_work_init(mg_work *work, mg_work_domain *domains,
                           uint32_t domain_capacity, mg_work_slot *slots,
                           uint32_t capacity, uint32_t running_limit,
                           mg_lease_key control, mg_work_limits limits) {
    return init(work, domains, domain_capacity, slots, capacity, running_limit, control, limits, true);
}

mg_work_result mg_work_init_shared(mg_work *work, mg_work_domain *domains,
                                  uint32_t domain_capacity, mg_work_slot *slots,
                                  uint32_t capacity, uint32_t running_limit,
                                  mg_lease_key owner, mg_work_limits limits) {
    return init(work, domains, domain_capacity, slots, capacity, running_limit, owner, limits, false);
}

mg_work_result mg_work_register(mg_work *work, uint32_t domain_slot,
                               mg_lease_key key, mg_work_limits limits) {
    if (!work || domain_slot >= work->domain_capacity || !valid_key(key) ||
        !limits.queued || !limits.running || !limits.cpu_ns)
        return MG_WORK_INVALID;
    if (work->domains[domain_slot].registered) return MG_WORK_BUSY;
    for (uint32_t i = 0; i < work->domain_capacity; ++i) {
        mg_work_domain *domain = &work->domains[i];
        if (domain->registered && domain->key.scope == key.scope &&
            domain->key.resource == key.resource) return MG_WORK_BUSY;
    }
    if (!fits(limits, work->capacity - work->guaranteed_slots,
              work->running_limit - work->guaranteed_running) ||
        work->shared_slots > work->capacity - work->guaranteed_slots - limits.queued - limits.running ||
        work->shared_running > work->running_limit - work->guaranteed_running - limits.running)
        return MG_WORK_FULL;
    register_domain(work, domain_slot, key, limits, true);
    return MG_WORK_OK;
}

mg_work_result mg_work_register_shared(mg_work *work, uint32_t domain_slot,
                                       mg_lease_key key, mg_work_limits limits) {
    if (!work || domain_slot >= work->domain_capacity || !valid_key(key) ||
        !limits.queued || !limits.running || !limits.cpu_ns ||
        limits.queued > work->capacity || limits.running > work->running_limit)
        return MG_WORK_INVALID;
    if (work->domains[domain_slot].registered) return MG_WORK_BUSY;
    for (uint32_t i = 0; i < work->domain_capacity; ++i) {
        mg_work_domain *domain = &work->domains[i];
        if (domain->registered && domain->key.scope == key.scope &&
            domain->key.resource == key.resource) return MG_WORK_BUSY;
    }
    register_domain(work, domain_slot, key, limits, false);
    return MG_WORK_OK;
}

mg_work_result mg_work_enqueue(mg_work *work, mg_work_domain_ref ref, uint64_t now_ns,
                              uint64_t deadline_ns, uint64_t allowance_ns,
                              mg_work_ticket *ticket) {
    if (!work || !ticket || !allowance_ns || now_ns < work->now_ns)
        return MG_WORK_INVALID;
    mg_work_domain *domain = domain_for(work, ref);
    if (!domain) return MG_WORK_STALE;
    if (!domain->active) return MG_WORK_CLOSED;
    if (deadline_ns <= now_ns) return MG_WORK_EXPIRED;
    if (domain->blocked || domain->consumed_ns > domain->limits.cpu_ns ||
        domain->reserved_ns > domain->limits.cpu_ns - domain->consumed_ns ||
        allowance_ns > domain->limits.cpu_ns - domain->consumed_ns -
                       domain->reserved_ns) return MG_WORK_BUDGET;
    if (domain->queued == domain->limits.queued || work->free_slot == NO_SLOT ||
        (domain->shared && work->shared_slots >= work->capacity - work->guaranteed_slots))
        return MG_WORK_FULL;
    if (!work->next_serial) return MG_WORK_EXHAUSTED;
    uint32_t index = work->free_slot;
    mg_work_slot *slot = &work->slots[index];
    uint64_t serial = work->next_serial;
    work->next_serial = serial == UINT64_MAX ? 0 : serial + 1;
    work->free_slot = slot->next;
    *slot = (mg_work_slot){.serial = serial, .deadline_ns = deadline_ns,
        .allowance_ns = allowance_ns,
        .domain_slot = ref.slot,
        .previous = domain->last, .next = NO_SLOT, .state = MG_WORK_QUEUED};
    if (domain->last != NO_SLOT) work->slots[domain->last].next = index;
    else domain->first = index;
    domain->last = index;
    ++domain->queued;
    if (domain->shared) ++work->shared_slots;
    domain->reserved_ns += allowance_ns;
    work->now_ns = now_ns;
    *ticket = (mg_work_ticket){.domain = ref.key, .serial = serial, .slot = index};
    return MG_WORK_OK;
}

static void unlink_queued(mg_work *work, mg_work_domain *domain,
                          mg_work_slot *slot) {
    if (slot->previous != NO_SLOT) work->slots[slot->previous].next = slot->next;
    else domain->first = slot->next;
    if (slot->next != NO_SLOT) work->slots[slot->next].previous = slot->previous;
    else domain->last = slot->previous;
    --domain->queued;
}

static void release_slot(mg_work *work, mg_work_domain *domain, uint32_t index) {
    mg_work_slot *slot = &work->slots[index];
    domain->reserved_ns -= slot->allowance_ns;
    if (domain->shared) --work->shared_slots;
    slot->state = MG_WORK_FREE;
    slot->serial = 0;
    slot->next = work->free_slot;
    work->free_slot = index;
}

mg_work_result mg_work_next(mg_work *work, uint64_t now_ns, mg_work_event *event) {
    if (!work || !event || now_ns < work->now_ns) return MG_WORK_INVALID;
    uint32_t index = work->cursor;
    for (uint32_t visited = 0; visited < work->domain_capacity; ++visited) {
        mg_work_domain *domain = &work->domains[index];
        uint32_t following = index == work->domain_capacity - 1 ? 0 : index + 1;
        if (domain->registered && domain->active && domain->first != NO_SLOT) {
            uint32_t slot_index = domain->first;
            mg_work_slot *slot = &work->slots[slot_index];
            bool expired = now_ns >= slot->deadline_ns;
            if (expired || (!domain->blocked &&
                            domain->running < domain->limits.running &&
                            work->running < work->running_limit &&
                            (!domain->shared || work->shared_running < work->running_limit - work->guaranteed_running))) {
                mg_work_event result = {.kind = expired ? MG_WORK_EXPIRE : MG_WORK_START,
                    .ticket = {.domain = domain->key, .serial = slot->serial,
                               .slot = slot_index}};
                unlink_queued(work, domain, slot);
                if (expired) release_slot(work, domain, slot_index);
                else {
                    slot->state = MG_WORK_RUNNING;
                    ++domain->running;
                    ++work->running;
                    if (domain->shared) ++work->shared_running;
                }
                work->cursor = following;
                work->now_ns = now_ns;
                *event = result;
                return MG_WORK_OK;
            }
        }
        index = following;
    }
    work->now_ns = now_ns;
    *event = (mg_work_event){.kind = MG_WORK_NONE};
    return MG_WORK_OK;
}

mg_work_result mg_work_cancel(mg_work *work, mg_work_ticket ticket) {
    if (!work) return MG_WORK_INVALID;
    mg_work_slot *slot = slot_for(work, ticket);
    if (!slot) return MG_WORK_STALE;
    if (slot->state != MG_WORK_QUEUED) return MG_WORK_BUSY;
    mg_work_domain *domain = &work->domains[slot->domain_slot];
    unlink_queued(work, domain, slot);
    release_slot(work, domain, ticket.slot);
    return MG_WORK_OK;
}

mg_work_result mg_work_stop(mg_work *work, mg_work_domain_ref ref, uint32_t *canceled) {
    if (!work || !canceled) return MG_WORK_INVALID;
    mg_work_domain *domain = domain_for(work, ref);
    if (!domain) return MG_WORK_STALE;
    uint32_t count = domain->queued;
    while (domain->first != NO_SLOT) {
        uint32_t index = domain->first;
        unlink_queued(work, domain, &work->slots[index]);
        release_slot(work, domain, index);
    }
    domain->active = false;
    *canceled = count;
    return MG_WORK_OK;
}

mg_work_result mg_work_finish(mg_work *work, mg_work_ticket ticket,
                             uint64_t now_ns, uint64_t measured_cpu_ns) {
    if (!work || now_ns < work->now_ns) return MG_WORK_INVALID;
    mg_work_slot *slot = slot_for(work, ticket);
    if (!slot) return MG_WORK_STALE;
    if (slot->state != MG_WORK_RUNNING) return MG_WORK_BUSY;
    mg_work_domain *domain = &work->domains[slot->domain_slot];
    bool overrun = measured_cpu_ns > slot->allowance_ns;
    bool overflow = domain->overflow ||
                    measured_cpu_ns > UINT64_MAX - domain->consumed_ns;
    domain->consumed_ns = overflow ? UINT64_MAX : domain->consumed_ns + measured_cpu_ns;
    domain->overflow = overflow;
    domain->overrun = domain->overrun || overrun;
    domain->blocked = domain->blocked || overrun || overflow;
    --domain->running;
    --work->running;
    if (domain->shared) --work->shared_running;
    release_slot(work, domain, ticket.slot);
    work->now_ns = now_ns;
    return overflow ? MG_WORK_OVERFLOW : overrun ? MG_WORK_OVERRUN : MG_WORK_OK;
}

mg_work_result mg_work_raise_limit(mg_work *work, mg_work_domain_ref ref, uint64_t cpu_ns) {
    if (!work) return MG_WORK_INVALID;
    mg_work_domain *domain = domain_for(work, ref);
    if (!domain) return MG_WORK_STALE;
    if (cpu_ns < domain->limits.cpu_ns) return MG_WORK_INVALID;
    if (domain->overflow) return MG_WORK_EXHAUSTED;
    if (cpu_ns == domain->limits.cpu_ns) return MG_WORK_OK;
    if (domain->consumed_ns > cpu_ns ||
        domain->reserved_ns > cpu_ns - domain->consumed_ns) return MG_WORK_BUDGET;
    domain->limits.cpu_ns = cpu_ns;
    domain->blocked = false;
    return MG_WORK_OK;
}

mg_work_result mg_work_unblock(mg_work *work, mg_work_domain_ref ref) {
    if (!work) return MG_WORK_INVALID;
    mg_work_domain *domain = domain_for(work, ref);
    if (!domain) return MG_WORK_STALE;
    if (domain->overflow) return MG_WORK_EXHAUSTED;
    if (domain->consumed_ns > domain->limits.cpu_ns ||
        domain->reserved_ns > domain->limits.cpu_ns - domain->consumed_ns)
        return MG_WORK_BUDGET;
    domain->blocked = false;
    return MG_WORK_OK;
}

mg_work_result mg_work_rearm(mg_work *work, mg_work_domain_ref ref, mg_work_domain_ref *next) {
    if (!work || !next) return MG_WORK_INVALID;
    mg_work_domain *domain = domain_for(work, ref);
    if (!domain) return MG_WORK_STALE;
    if (domain->active || domain->queued || domain->running) return MG_WORK_BUSY;
    if (domain->key.generation == UINT64_MAX) return MG_WORK_EXHAUSTED;
    ++domain->key.generation;
    domain->active = true;
    *next = (mg_work_domain_ref){.key = domain->key, .slot = ref.slot};
    return MG_WORK_OK;
}
