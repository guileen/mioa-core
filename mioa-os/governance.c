/* SPDX-License-Identifier: MIT */
#include "governance.h"
#include <limits.h>
#include <string.h>

static uint32_t weight(mg_gov_class c) {
    switch (c) { case MG_GOV_FOREGROUND: return 8; case MG_GOV_VISIBLE: return 4;
        case MG_GOV_BACKGROUND: return 1; case MG_GOV_BACKGROUND_AUTHORIZED: return 2; }
    return 0;
}
static int64_t add_cap(int64_t value, uint64_t quota) {
    int64_t cap = (int64_t)(quota * 2);
    if (value >= cap - (int64_t)quota) return cap;
    return value + (int64_t)quota;
}
static int64_t debit(int64_t value, uint64_t amount) {
    if (amount > INT64_MAX || value < INT64_MIN + (int64_t)amount) return INT64_MIN;
    return value - (int64_t)amount;
}
static uint64_t charge(uint64_t cpu, uint32_t w) {
    __uint128_t n = (__uint128_t)cpu * 1024 + w - 1;
    n /= w;
    return n > UINT64_MAX ? UINT64_MAX : (uint64_t)n;
}
static uint64_t io_burst(uint32_t w) {
    return (w >= 4 ? UINT64_C(2) : UINT64_C(1)) * 1024 * 1024;
}
static uint64_t io_rate(uint32_t w) {
    return (w >= 8 ? UINT64_C(16) : w >= 4 ? UINT64_C(8) : UINT64_C(1)) * 1024 * 1024;
}
static void io_refill(mg_gov_io_bucket *bucket,uint64_t elapsed,uint64_t rate,uint64_t burst) {
    __uint128_t credit=(__uint128_t)elapsed*rate+bucket->remainder;
    __uint128_t whole=credit/UINT64_C(1000000000);
    if (bucket->tokens>=burst || whole>=burst-bucket->tokens) {
        bucket->tokens=burst; bucket->remainder=0;
    } else {
        bucket->tokens+=(uint64_t)whole;
        bucket->remainder=(uint64_t)(credit%UINT64_C(1000000000));
    }
}
static uint64_t runnable_baseline(const mg_governance *g, uint32_t exclude) {
    uint64_t base = UINT64_MAX;
    for (uint32_t i = 0; i < MG_GOV_DOMAINS; ++i) {
        const mg_gov_domain *d = &g->domains[i];
        if (i != exclude && d->registered && !d->stopping &&
            (d->count || g->ledger_domains[i + 1].running) && d->vruntime < base)
            base = d->vruntime;
    }
    return base;
}
static mg_work_result advance(mg_governance *g, uint64_t now) {
    if (!g || now < g->now_ns) return MG_WORK_INVALID;
    if (now - g->period_start_ns >= MG_GOV_PERIOD_NS) {
        uint64_t periods = (now - g->period_start_ns) / MG_GOV_PERIOD_NS;
        g->period_start_ns += periods * MG_GOV_PERIOD_NS;
        /* A delayed owner turn cannot accumulate more than one refill. */
        g->pool_tokens = add_cap(g->pool_tokens, g->pool_quota_ns);
        for (uint32_t i = 0; i < MG_GOV_DOMAINS; ++i)
            if (g->domains[i].registered)
                g->domains[i].tokens = add_cap(g->domains[i].tokens, g->domains[i].quota_ns);
    }
    g->now_ns = now;
    return MG_WORK_OK;
}
mg_work_result mg_gov_init(mg_governance *g, uint32_t concurrency, uint64_t now, mg_lease_key control) {
    if (!g || !concurrency || concurrency > 4) return MG_WORK_INVALID;
    memset(g, 0, sizeof(*g));
    g->running_limit = concurrency;
    /* 80% of the 80%-CPU envelope is the app pool. */
    g->pool_quota_ns = UINT64_C(64000000) * concurrency;
    g->pool_tokens = (int64_t)g->pool_quota_ns;
    g->period_start_ns = g->now_ns = now;
    g->io_clock_ns=now; g->next_io=1;
    for (uint32_t i=0;i<MG_GOV_IO_KINDS;i++) g->io[i].tokens=UINT64_C(8)*1024*1024;
    /* The sidecar monitor is outside this application execution ledger. */
    return mg_work_init_shared(&g->ledger, g->ledger_domains, MG_GOV_DOMAINS + 1,
                               g->ledger_slots, MG_GOV_COMPLETIONS + 1, concurrency,
                               control, (mg_work_limits){1, 1, UINT64_MAX});
}
mg_work_result mg_gov_register(mg_governance *g, uint32_t i, mg_lease_key key,
                               uint64_t quota, mg_gov_class c, uint64_t policy, uint64_t cap) {
    if (!g || i >= MG_GOV_DOMAINS || !quota || quota > INT64_MAX / 2 ||
        !weight(c) || !policy || !cap || g->domains[i].registered) return MG_WORK_INVALID;
    mg_work_result r = mg_work_register_shared(&g->ledger, i + 1, key,
                                                (mg_work_limits){1, 1, UINT64_MAX});
    if (r != MG_WORK_OK) return r;
    uint64_t base = runnable_baseline(g, i);
    g->domains[i] = (mg_gov_domain){.ref = {key, i + 1}, .weight = weight(c),
        .quota_ns = quota, .tokens = (int64_t)quota, .policy_epoch = policy,
        .cap_epoch = cap, .vruntime = base == UINT64_MAX ? 0 : base, .registered = true};
    g->domains[i].io_clock_ns=g->now_ns;
    for (uint32_t j=0;j<MG_GOV_IO_KINDS;j++) g->domains[i].io[j].tokens=io_burst(weight(c));
    return MG_WORK_OK;
}
mg_work_result mg_gov_policy(mg_governance *g, uint32_t i, mg_gov_class c, uint64_t policy, uint64_t cap, uint64_t now) {
    if (!g || i >= MG_GOV_DOMAINS || !g->domains[i].registered || !weight(c) ||
        policy <= g->domains[i].policy_epoch || cap < g->domains[i].cap_epoch) return MG_WORK_INVALID;
    mg_gov_domain *d = &g->domains[i];
    if (d->stopping) return MG_WORK_CLOSED;
    /* Settle elapsed time under the old class at the policy transition. */
    mg_work_result r = mg_gov_io_refresh(g, i, now);
    if (r != MG_WORK_OK) return r;
    d->weight = weight(c); d->policy_epoch = policy; d->cap_epoch = cap;
    uint64_t burst = io_burst(d->weight);
    for (uint32_t j=0;j<MG_GOV_IO_KINDS;j++) {
        if (d->io[j].tokens >= burst) {
            d->io[j].tokens = burst;
            d->io[j].remainder = 0;
        }
    }
    return MG_WORK_OK;
}
mg_work_result mg_gov_submit(mg_governance *g, uint32_t i, mg_gov_request request) {
    if (!g || i >= MG_GOV_DOMAINS || !g->domains[i].registered || !request.id ||
        !request.estimate_ns || request.estimate_ns > INT64_MAX || !request.deadline_ns)
        return MG_WORK_INVALID;
    mg_gov_domain *d = &g->domains[i];
    if (d->stopping) return MG_WORK_CLOSED;
    if (request.policy_epoch != d->policy_epoch || request.cap_epoch != d->cap_epoch) return MG_WORK_STALE;
    if (d->count == MG_GOV_READY) return MG_WORK_FULL;
    if (!d->count && !g->ledger_domains[i + 1].running && !d->recently_finished) {
        uint64_t base = runnable_baseline(g, i);
        if (base != UINT64_MAX && d->vruntime < base) d->vruntime = base;
    }
    d->recently_finished = false;
    d->ready[(d->first + d->count) % MG_GOV_READY] = request;
    ++d->count;
    return MG_WORK_OK;
}
mg_work_result mg_gov_next(mg_governance *g, uint64_t now, mg_gov_cpu_fn sample, void *context,
                           uint32_t *out_domain, uint32_t *out_slot, mg_gov_request *out) {
    if (!g || !sample || !out_domain || !out_slot || !out) return MG_WORK_INVALID;
    mg_work_result r = advance(g, now); if (r != MG_WORK_OK) return r;
    /* A completion followed immediately by a new request is continuously
     * runnable. Only an intervening scheduler turn makes it a sleeper. */
    for (uint32_t i=0; i<MG_GOV_DOMAINS; ++i)
        if (!g->domains[i].count && !g->ledger_domains[i+1].running)
            g->domains[i].recently_finished=false;
    uint32_t best = UINT32_MAX, cell = UINT32_MAX; uint64_t best_v = UINT64_MAX;
    for (uint32_t k = 0; k < MG_GOV_DOMAINS; ++k) {
        uint32_t i = (g->cursor + k) % MG_GOV_DOMAINS; mg_gov_domain *d = &g->domains[i];
        if (!d->registered || !d->count) continue;
        mg_gov_request *q = &d->ready[d->first];
        if (d->stopping || now >= q->deadline_ns || q->policy_epoch != d->policy_epoch || q->cap_epoch != d->cap_epoch) {
            *out_domain = i; *out_slot = UINT32_MAX; *out = *q;
            d->first = (d->first + 1) % MG_GOV_READY; --d->count; g->cursor = (i + 1) % MG_GOV_DOMAINS;
            return d->stopping ? MG_WORK_CLOSED : now >= q->deadline_ns ? MG_WORK_EXPIRED : MG_WORK_STALE;
        }
        if (g->ledger.running >= g->running_limit || g->ledger_domains[i + 1].running ||
            d->tokens < 0 || g->pool_tokens < 0 || q->estimate_ns > (uint64_t)d->tokens ||
            q->estimate_ns > (uint64_t)g->pool_tokens) continue;
        if (best == UINT32_MAX || d->vruntime < best_v) { best = i; best_v = d->vruntime; }
    }
    if (best == UINT32_MAX) return MG_WORK_BUSY;
    for (uint32_t i = 0; i < MG_GOV_COMPLETIONS; ++i) if (!g->running[i].used) { cell = i; break; }
    if (cell == UINT32_MAX) return MG_WORK_FULL;
    mg_gov_domain *d = &g->domains[best]; mg_gov_request q = d->ready[d->first];
    uint32_t w = d->weight == 2 && !q.authorized_kind ? 1 : d->weight;
    uint64_t v = charge(q.estimate_ns, w);
    if (v == UINT64_MAX || d->vruntime > UINT64_MAX - v) return MG_WORK_EXHAUSTED;
    uint64_t cpu_start;
    if (!sample(context, best, &cpu_start)) return MG_WORK_BUDGET;
    mg_work_ticket ticket;
    r = mg_work_enqueue(&g->ledger, d->ref, now, q.deadline_ns, q.estimate_ns, &ticket);
    if (r != MG_WORK_OK) return r;
    mg_work_event event; r = mg_work_next(&g->ledger, now, &event);
    if (r != MG_WORK_OK || event.kind != MG_WORK_START || event.ticket.serial != ticket.serial) {
        mg_work_cancel(&g->ledger, ticket); return r == MG_WORK_OK ? MG_WORK_BUSY : r;
    }
    d->tokens = debit(d->tokens, q.estimate_ns); g->pool_tokens = debit(g->pool_tokens, q.estimate_ns);
    d->vruntime += v; d->first = (d->first + 1) % MG_GOV_READY; --d->count;
    g->running[cell] = (mg_gov_running){.used=true, .domain=best, .id=q.id, .estimate_ns=q.estimate_ns,
        .started_ns=now, .last_sample_ns=now, .cpu_start_ns=cpu_start,
        .last_cpu_ns=cpu_start, .weight=w, .ticket=ticket};
    g->cursor = (best + 1) % MG_GOV_DOMAINS;
    *out_domain = best; *out_slot = cell; *out = q; return MG_WORK_OK;
}
mg_work_result mg_gov_finish(mg_governance *g, uint32_t slot, uint64_t now, uint64_t actual) {
    if (!g || slot >= MG_GOV_COMPLETIONS || !g->running[slot].used) return MG_WORK_STALE;
    mg_work_result r = advance(g, now); if (r != MG_WORK_OK) return r;
    mg_gov_running *run = &g->running[slot]; mg_gov_domain *d = &g->domains[run->domain];
    uint64_t old = charge(run->estimate_ns, run->weight), cost = charge(actual, run->weight);
    r = mg_work_finish(&g->ledger, run->ticket, now, actual);
    if (r != MG_WORK_OK && r != MG_WORK_OVERRUN && r != MG_WORK_OVERFLOW) return r;
    if (actual > run->estimate_ns) { uint64_t extra = actual - run->estimate_ns;
        d->tokens = debit(d->tokens, extra); g->pool_tokens = debit(g->pool_tokens, extra);
    } else { uint64_t refund = run->estimate_ns - actual;
        d->tokens = d->tokens > INT64_MAX - (int64_t)refund ? INT64_MAX : d->tokens + (int64_t)refund;
        g->pool_tokens = g->pool_tokens > INT64_MAX - (int64_t)refund ? INT64_MAX : g->pool_tokens + (int64_t)refund;
        if (d->tokens > (int64_t)(d->quota_ns * 2)) d->tokens = (int64_t)(d->quota_ns * 2);
        if (g->pool_tokens > (int64_t)(g->pool_quota_ns * 2)) g->pool_tokens = (int64_t)(g->pool_quota_ns * 2);
    }
    d->vruntime -= old;
    d->vruntime = cost == UINT64_MAX || d->vruntime > UINT64_MAX - cost ? UINT64_MAX : d->vruntime + cost;
    d->recently_finished = !d->count;
    run->used = false;
    if (r == MG_WORK_OVERRUN) (void)mg_work_unblock(&g->ledger, d->ref);
    return r;
}
mg_work_result mg_gov_stop(mg_governance *g, uint32_t i) {
    if (!g || i >= MG_GOV_DOMAINS || !g->domains[i].registered) return MG_WORK_INVALID;
    mg_gov_domain *d = &g->domains[i]; d->stopping = true;
    uint32_t canceled; return mg_work_stop(&g->ledger, d->ref, &canceled);
}
mg_work_result mg_gov_rearm(mg_governance *g, uint32_t i, mg_work_domain_ref *next) {
    if (!g || !next || i >= MG_GOV_DOMAINS || !g->domains[i].registered)
        return MG_WORK_INVALID;
    mg_gov_domain *d = &g->domains[i];
    if (!d->stopping) return MG_WORK_BUSY;
    if (d->io_running) return MG_WORK_BUSY;
    for (uint32_t j=0; j<MG_GOV_COMPLETIONS; ++j)
        if (g->running[j].used && g->running[j].domain == i) return MG_WORK_BUSY;
    mg_work_domain_ref ref;
    mg_work_result r = mg_work_rearm(&g->ledger, d->ref, &ref);
    if (r != MG_WORK_OK) return r;
    d->ref = ref;
    /* Old, unadmitted requests are canceled, never inherited by the new child. */
    d->first = d->count = 0;
    d->stopping = d->recently_finished = false;
    *next = ref;
    return MG_WORK_OK;
}
mg_work_result mg_gov_sample(mg_governance *g, uint32_t slot, uint64_t now, uint64_t cpu,
                             mg_gov_stop_fn stop, void *ctx) {
    if (!g || slot >= MG_GOV_COMPLETIONS || !g->running[slot].used || !stop) return MG_WORK_INVALID;
    mg_gov_running *r = &g->running[slot];
    if (now < g->now_ns || cpu < r->last_cpu_ns || cpu < r->cpu_start_ns) return MG_WORK_INVALID;
    mg_work_result clock_result = advance(g, now);
    if (clock_result != MG_WORK_OK) return clock_result;
    r->last_cpu_ns = cpu;
    uint64_t used = cpu - r->cpu_start_ns;
    if (now - r->last_sample_ns >= MG_GOV_PERIOD_NS) {
        r->last_sample_ns = now;
        if (now - r->started_ns > UINT64_C(2000000) && r->samples < UINT32_MAX) ++r->samples;
    }
    mg_gov_domain *d = &g->domains[r->domain];
    uint64_t remaining = d->tokens > 0 ? (uint64_t)d->tokens : 0;
    uint64_t pool = g->pool_tokens > 0 ? (uint64_t)g->pool_tokens : 0;
    if (!d->stopping && (r->samples >= 2 || (used > r->estimate_ns &&
        (used - r->estimate_ns > remaining || used - r->estimate_ns > pool)))) {
        uint32_t domain = r->domain; mg_work_result result = mg_gov_stop(g, domain);
        if (result != MG_WORK_OK) return result;
        stop(ctx, domain);
    }
    return MG_WORK_OK;
}
mg_work_result mg_gov_authorize(mg_gov_dispatch_owner *o, uint64_t epoch, uint64_t *seq) {
    if (!o || !seq || !epoch || epoch != o->epoch) return MG_WORK_STALE;
    if (o->next_sequence == UINT64_MAX) return MG_WORK_EXHAUSTED;
    for (uint32_t i=0; i<MG_GOV_COMPLETIONS; ++i) if (!o->active[i]) {
        *seq = ++o->next_sequence; o->active[i] = *seq; return MG_WORK_OK;
    }
    return MG_WORK_FULL;
}
mg_work_result mg_gov_fence(mg_gov_dispatch_owner *o, uint64_t epoch, uint64_t *watermark) {
    if (!o || !watermark || !epoch || epoch <= o->epoch) return MG_WORK_INVALID;
    o->epoch = epoch; *watermark = o->next_sequence; return MG_WORK_OK;
}
mg_work_result mg_gov_settle(mg_gov_dispatch_owner *o, uint64_t sequence) {
    if (!o || !sequence) return MG_WORK_INVALID;
    for (uint32_t i=0; i<MG_GOV_COMPLETIONS; ++i) if (o->active[i] == sequence) {
        o->active[i] = 0; return MG_WORK_OK;
    }
    return MG_WORK_STALE;
}

mg_work_result mg_gov_io_refresh(mg_governance *g,uint32_t i,uint64_t now) {
    if (!g || i>=MG_GOV_DOMAINS || !g->domains[i].registered ||
        now<g->now_ns || now<g->io_clock_ns || now<g->domains[i].io_clock_ns)
        return MG_WORK_INVALID;
    mg_work_result r=advance(g,now); if (r!=MG_WORK_OK) return r;
    mg_gov_domain *d=&g->domains[i];
    for (uint32_t j=0;j<MG_GOV_IO_KINDS;j++) {
        io_refill(&g->io[j],now-g->io_clock_ns,UINT64_C(32)*1024*1024,UINT64_C(8)*1024*1024);
        io_refill(&d->io[j],now-d->io_clock_ns,io_rate(d->weight),io_burst(d->weight));
    }
    g->io_clock_ns=d->io_clock_ns=now;
    return MG_WORK_OK;
}
mg_work_result mg_gov_io_begin(mg_governance *g,uint32_t i,mg_gov_io_kind kind,
                               uint64_t bytes,uint64_t now,uint64_t *ticket) {
    if (!g || !ticket || i>=MG_GOV_DOMAINS || !g->domains[i].registered ||
        (unsigned)kind>=MG_GOV_IO_KINDS || !bytes || bytes>MG_GOV_IO_CHUNK)
        return MG_WORK_INVALID;
    mg_gov_domain *d=&g->domains[i];
    if (d->stopping) return MG_WORK_CLOSED;
    mg_work_result r=mg_gov_io_refresh(g,i,now); if (r!=MG_WORK_OK) return r;
    uint32_t limit=d->weight>=8?6:d->weight>=4?4:2;
    if (g->io_running==MG_GOV_IO_SLOTS || d->io_running>=limit) return MG_WORK_FULL;
    if (bytes>g->io[kind].tokens || bytes>d->io[kind].tokens) return MG_WORK_BUDGET;
    if (!g->next_io) return MG_WORK_EXHAUSTED;
    uint32_t slot=0;
    while (slot<MG_GOV_IO_SLOTS && g->io_uses[slot].active) slot++;
    if (slot==MG_GOV_IO_SLOTS) return MG_WORK_FULL;
    uint64_t serial=g->next_io;
    g->next_io=serial==UINT64_MAX?0:serial+1;
    g->io_uses[slot]=(mg_gov_io_use){.serial=serial,.bytes=bytes,.domain=i,.kind=kind,.active=true};
    g->io[kind].tokens-=bytes; d->io[kind].tokens-=bytes;
    g->io_running++; d->io_running++; *ticket=serial;
    return MG_WORK_OK;
}
mg_work_result mg_gov_io_finish(mg_governance *g,uint32_t i,uint64_t ticket,
                                uint64_t actual,uint64_t now) {
    if (!g || i>=MG_GOV_DOMAINS || !g->domains[i].registered || !ticket) return MG_WORK_INVALID;
    mg_gov_io_use *use=NULL;
    for (uint32_t j=0;j<MG_GOV_IO_SLOTS;j++) if (g->io_uses[j].active && g->io_uses[j].serial==ticket) {
        use=&g->io_uses[j]; break;
    }
    if (!use || use->domain!=i) return MG_WORK_STALE;
    if (actual>use->bytes) return MG_WORK_INVALID;
    mg_work_result r=mg_gov_io_refresh(g,i,now); if (r!=MG_WORK_OK) return r;
    mg_gov_domain *d=&g->domains[i];
    uint64_t unused=use->bytes-actual;
    uint64_t root_burst=UINT64_C(8)*1024*1024, domain_burst=io_burst(d->weight);
    mg_gov_io_bucket *root=&g->io[use->kind], *domain=&d->io[use->kind];
    if (unused>=root_burst-root->tokens) { root->tokens=root_burst; root->remainder=0; }
    else root->tokens+=unused;
    if (unused>=domain_burst-domain->tokens) { domain->tokens=domain_burst; domain->remainder=0; }
    else domain->tokens+=unused;
    uint64_t *total=&g->io_bytes[use->kind];
    if (actual>UINT64_MAX-*total) { *total=UINT64_MAX; g->io_counter_overflow=true; }
    else *total+=actual;
    use->active=false; g->io_running--; d->io_running--;
    return MG_WORK_OK;
}
