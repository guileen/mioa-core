/* SPDX-License-Identifier: MIT */
#include "memory_grant.h"
#include <errno.h>
#include <string.h>
#include <sys/socket.h>

/* MGR1 wire: request = magic[4], op:u32, request:u64, grant:u64,
 * host_bytes:u64, handles:u64; reply = magic[4], result:u32, request:u64,
 * grant_or_ceiling:u64, reserved_zero:u64. Integers are big endian. op1
 * commits one pre-reserved baseline; op2 closes a token; op3 reserves an
 * anonymous unlinked spool (disk bytes, at most one handle); op4 uses the base
 * token to reserve heap growth; op5 reports the trusted ceiling, not quota.
 * Prelaunch opt-in permits op6 to return physically disposed native growth
 * chunks using the baseline nonce; the final reply word remains zero.
 * Otherwise heap tokens retire only after closure AND trusted process exit.
 * Spool tokens release after trusted fclose/unlink or exact process exit,
 * since this binding forbids linked files, exported descriptors and surviving maps. */
static uint64_t get64(const unsigned char *p) {
    uint64_t v=0; for (unsigned i=0;i<8;++i) v=(v<<8)|p[i]; return v;
}
static uint32_t get32(const unsigned char *p) {
    uint32_t v=0; for (unsigned i=0;i<4;++i) v=(v<<8)|p[i]; return v;
}
static void put64(unsigned char *p,uint64_t v) {
    for (int i=7;i>=0;--i) { p[i]=(unsigned char)v; v>>=8; }
}
static void put32(unsigned char *p,uint32_t v) {
    for (int i=3;i>=0;--i) { p[i]=(unsigned char)v; v>>=8; }
}
mg_memory_result mg_memory_grant_init(mg_memory_grant *g, mg_memory *memory, int fd,
                                      mg_lease_key account, mg_lease_key resource,
                                      mg_lease_key domain, uint64_t launch_id) {
    if (!g || !memory || fd<0 || !account.scope || !account.resource || !account.generation ||
        !resource.scope || !resource.resource || !resource.generation ||
        !domain.scope || !domain.resource || !domain.generation || !launch_id ||
        account.scope!=memory->key.scope || account.resource!=memory->key.resource ||
        account.generation!=memory->key.generation) return MG_MEMORY_INVALID;
#ifdef __APPLE__
    int enabled=1;
    if (setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&enabled,sizeof(enabled))<0) return MG_MEMORY_INVALID;
#endif
    memset(g,0,sizeof(*g)); g->memory=memory; g->fd=fd; g->account=account; g->resource=resource;
    g->domain=domain; g->launch_id=launch_id;
    return MG_MEMORY_OK;
}
static bool prelaunch_configurable(const mg_memory_grant *g) {
    return g && g->memory && !g->wire_started && !g->last_request &&
        !g->has_ticket && !g->closure_asserted && !g->exit_observed && !g->closed;
}
mg_memory_result mg_memory_grant_set_heap_ceiling(mg_memory_grant *g, uint64_t bytes) {
    if (!prelaunch_configurable(g) || bytes<MG_MEMORY_GRANT_HEAP_CHUNK ||
        bytes%MG_MEMORY_GRANT_HEAP_CHUNK ||
        bytes<g->pre_reserved.value[MG_MEMORY_HOST_BYTES] ||
        (g->has_reservation && (g->pre_reserved.value[MG_MEMORY_HOST_BYTES]!=MG_MEMORY_GRANT_HEAP_CHUNK ||
                                g->pre_reserved.value[MG_MEMORY_HANDLES])))
        return MG_MEMORY_INVALID;
    g->heap_ceiling=bytes;
    return MG_MEMORY_OK;
}
mg_memory_result mg_memory_grant_allow_trusted_heap_release(mg_memory_grant *g) {
    if (!prelaunch_configurable(g) || !g->heap_ceiling)
        return MG_MEMORY_INVALID;
    g->trusted_heap_release=true;
    return MG_MEMORY_OK;
}
mg_memory_result mg_memory_grant_pre_reserve(mg_memory_grant *g, uint64_t bytes, uint64_t handles) {
    if (!prelaunch_configurable(g) || !bytes || g->has_reservation ||
        (g->heap_ceiling && (bytes!=MG_MEMORY_GRANT_HEAP_CHUNK || handles)))
        return MG_MEMORY_INVALID;
    mg_memory_amount amount={0}; mg_memory_ticket ticket;
    amount.value[MG_MEMORY_HOST_BYTES]=bytes; amount.value[MG_MEMORY_HANDLES]=handles;
    mg_memory_result r=mg_memory_reserve(g->memory,g->account,g->resource,amount,
                                         MG_MEMORY_ORDINARY,&ticket);
    if (r==MG_MEMORY_OK) { g->ticket=ticket; g->pre_reserved=amount; g->has_reservation=true; }
    return r;
}
mg_memory_result mg_memory_grant_cancel_prelaunch(mg_memory_grant *g) {
    if (!g || !g->memory || !g->has_reservation || g->has_ticket) return MG_MEMORY_INVALID;
    mg_memory_result r=mg_memory_cancel(g->memory,g->ticket);
    if (r==MG_MEMORY_OK) g->has_reservation=false;
    return r;
}
static bool same_key(mg_lease_key a, mg_lease_key b) {
    return a.scope==b.scope && a.resource==b.resource && a.generation==b.generation;
}
/* Read-only resolution is needed to prevalidate the complete op6 batch.
 * The owner serializes this endpoint with ALL access to the bound ledger. */
static const mg_memory_slot *bound_slot(const mg_memory_grant *g, mg_memory_ticket ticket) {
    if (!same_key(g->memory->key,g->account) || !same_key(ticket.account,g->account) ||
        !same_key(ticket.resource,g->resource) || !ticket.serial ||
        ticket.slot>=g->memory->slot_count)
        return NULL;
    const mg_memory_slot *slot=&g->memory->slots[ticket.slot];
    if (slot->serial!=ticket.serial || !same_key(slot->resource,g->resource) ||
        (slot->state!=MG_MEMORY_COMMITTED && slot->state!=MG_MEMORY_RETIRING))
        return NULL;
    return slot;
}
static mg_memory_result release_extra(mg_memory_grant *g, unsigned i) {
    uint64_t bytes=0;
    if (g->extra_kind[i]==2) {
        const mg_memory_slot *slot=bound_slot(g,g->extra[i]);
        if (!slot) return MG_MEMORY_STALE;
        bytes=slot->amount.value[MG_MEMORY_HOST_BYTES];
        if (bytes>g->granted_host_bytes) return MG_MEMORY_INVALID;
    }
    mg_memory_result r=mg_memory_retire(g->memory,g->extra[i]);
    if (r==MG_MEMORY_OK) r=mg_memory_release(g->memory,g->extra[i]);
    if (r==MG_MEMORY_OK) {
        g->granted_host_bytes-=bytes;
        g->extra[i]=(mg_memory_ticket){0};
        g->extra_kind[i]=0; g->extra_closed[i]=false;
    }
    return r;
}
static mg_memory_result release_heap_growth(mg_memory_grant *g, uint64_t bytes) {
    const mg_memory_slot *base=bound_slot(g,g->ticket);
    if (!base) return MG_MEMORY_STALE;
    uint64_t baseline=base->amount.value[MG_MEMORY_HOST_BYTES];
    if (g->granted_host_bytes<baseline || bytes>g->granted_host_bytes-baseline)
        return MG_MEMORY_INVALID;
    uint64_t remaining=bytes;
    unsigned end=0;
    for (;end<MG_MEMORY_GRANT_EXTRA && remaining;++end) {
        unsigned i=end;
        if (g->extra_kind[i]!=2) continue;
        const mg_memory_slot *slot=bound_slot(g,g->extra[i]);
        if (!slot) return MG_MEMORY_STALE;
        if (g->extra[i].serial==g->ticket.serial ||
            slot->amount.value[MG_MEMORY_HOST_BYTES]!=MG_MEMORY_GRANT_HEAP_CHUNK ||
            slot->amount.value[MG_MEMORY_HANDLES] ||
            slot->amount.value[MG_MEMORY_DEVICE_BYTES] ||
            slot->amount.value[MG_MEMORY_DISK_BYTES])
            return MG_MEMORY_INVALID;
        remaining-=MG_MEMORY_GRANT_HEAP_CHUNK;
    }
    if (remaining) return MG_MEMORY_INVALID;
    /* All selected tickets and the entire amount are valid before retirement.
     * With external serialization these existing ledger transitions cannot fail. */
    for (unsigned i=0;i<end;++i) {
        if (g->extra_kind[i]!=2) continue;
        mg_memory_result r=release_extra(g,i);
        if (r!=MG_MEMORY_OK) return r;
    }
    return MG_MEMORY_OK;
}
static mg_memory_result settle(mg_memory_grant *g) {
    if (!g->exit_observed) return MG_MEMORY_OK;
    if (g->has_reservation) {
        mg_memory_result r=mg_memory_cancel(g->memory,g->ticket);
        if (r!=MG_MEMORY_OK) return r;
        g->has_reservation=false;
    }
    for (unsigned i=0;i<MG_MEMORY_GRANT_EXTRA;++i) {
        if (g->extra_kind[i] && (g->extra_kind[i]==1 || g->extra_closed[i] || g->closure_asserted)) {
            mg_memory_result r=release_extra(g,i);
            if (r!=MG_MEMORY_OK) return r;
        }
    }
    if (!g->has_ticket || !g->closure_asserted) return MG_MEMORY_OK;
    mg_memory_result r=mg_memory_retire(g->memory,g->ticket);
    if (r!=MG_MEMORY_OK) return r;
    r=mg_memory_release(g->memory,g->ticket);
    if (r==MG_MEMORY_OK) { g->has_ticket=false; g->granted_host_bytes=0; }
    return r;
}
static mg_memory_result reserve_extra(mg_memory_grant *g, mg_memory_amount amount,
                                      unsigned char kind, uint64_t *token) {
    unsigned i=0;
    while (i<MG_MEMORY_GRANT_EXTRA && g->extra_kind[i]) ++i;
    if (i==MG_MEMORY_GRANT_EXTRA) return MG_MEMORY_FULL;
    mg_memory_ticket ticket;
    mg_memory_result r=mg_memory_reserve(g->memory,g->account,g->resource,amount,
                                         MG_MEMORY_ORDINARY,&ticket);
    if (r!=MG_MEMORY_OK) return r;
    r=mg_memory_commit(g->memory,ticket,amount);
    if (r!=MG_MEMORY_OK) { (void)mg_memory_cancel(g->memory,ticket); return r; }
    g->extra[i]=ticket; g->extra_kind[i]=kind; g->extra_closed[i]=false;
    *token=ticket.serial;
    return MG_MEMORY_OK;
}
static void process(mg_memory_grant *g) {
    const unsigned char *q=g->request; uint32_t op=get32(q+4);
    uint64_t request=get64(q+8), grant=get64(q+16), bytes=get64(q+24), handles=get64(q+32);
    mg_memory_result result=MG_MEMORY_INVALID; uint64_t out_grant=0;
    if (!memcmp(q,"MGR1",4) && request && request>g->last_request) {
        g->last_request=request;
        if (op==1 && !grant && !g->has_ticket && !g->closure_asserted && !g->exit_observed && bytes) {
            mg_memory_amount amount={0};
            amount.value[MG_MEMORY_HOST_BYTES]=bytes; amount.value[MG_MEMORY_HANDLES]=handles;
            if (g->heap_ceiling && (bytes!=MG_MEMORY_GRANT_HEAP_CHUNK || handles))
                result=MG_MEMORY_INVALID;
            else if (!g->has_reservation) result=MG_MEMORY_INVALID;
            else if (bytes>g->pre_reserved.value[MG_MEMORY_HOST_BYTES] ||
                     handles>g->pre_reserved.value[MG_MEMORY_HANDLES]) result=MG_MEMORY_LIMIT;
            else {
                result=mg_memory_commit(g->memory,g->ticket,amount);
                if (result==MG_MEMORY_OK) {
                    g->has_reservation=false; g->has_ticket=true; out_grant=g->ticket.serial;
                    g->granted_host_bytes=bytes;
                }
            }
        } else if (op==2 && g->has_ticket && !g->closure_asserted &&
                   grant==g->ticket.serial && !bytes && !handles) {
            g->closure_asserted=true; out_grant=grant; result=settle(g);
        } else if (op==2 && grant && !bytes && !handles) {
            unsigned i=0;
            while (i<MG_MEMORY_GRANT_EXTRA &&
                   (!g->extra_kind[i] || g->extra[i].serial!=grant)) ++i;
            if (i<MG_MEMORY_GRANT_EXTRA && !g->extra_closed[i]) {
                if (g->extra_kind[i]==1) result=release_extra(g,i);
                else { g->extra_closed[i]=true; result=settle(g); }
                if (result==MG_MEMORY_OK) out_grant=grant;
            }
        } else if (op==3 && !grant && bytes && handles<=1 &&
                   g->has_ticket && !g->closure_asserted && !g->exit_observed) {
            mg_memory_amount amount={0};
            amount.value[MG_MEMORY_DISK_BYTES]=bytes;
            amount.value[MG_MEMORY_HANDLES]=handles;
            result=reserve_extra(g,amount,1,&out_grant);
        } else if (op==4 && g->has_ticket && grant==g->ticket.serial && bytes && !handles &&
                   !g->closure_asserted && !g->exit_observed) {
            uint64_t limit=g->heap_ceiling ? g->heap_ceiling : g->pre_reserved.value[MG_MEMORY_HOST_BYTES];
            if (g->heap_ceiling && bytes!=MG_MEMORY_GRANT_HEAP_CHUNK)
                result=MG_MEMORY_INVALID;
            else if (g->granted_host_bytes>limit || bytes>limit-g->granted_host_bytes)
                result=MG_MEMORY_LIMIT;
            else {
                mg_memory_amount amount={0}; amount.value[MG_MEMORY_HOST_BYTES]=bytes;
                result=reserve_extra(g,amount,2,&out_grant);
                if (result==MG_MEMORY_OK) g->granted_host_bytes+=bytes;
            }
        } else if (op==5 && !grant && !bytes && !handles &&
                   !g->closure_asserted && !g->exit_observed) {
            out_grant=g->heap_ceiling ? g->heap_ceiling : g->pre_reserved.value[MG_MEMORY_HOST_BYTES];
            result=out_grant ? MG_MEMORY_OK : MG_MEMORY_INVALID;
        } else if (op==6 && g->trusted_heap_release && g->has_ticket &&
                   grant==g->ticket.serial && bytes && !handles &&
                   !(bytes%MG_MEMORY_GRANT_HEAP_CHUNK) &&
                   !g->closure_asserted && !g->exit_observed) {
            result=release_heap_growth(g,bytes);
            if (result==MG_MEMORY_OK) out_grant=grant;
        }
    }
    memset(g->reply,0,sizeof(g->reply)); memcpy(g->reply,"MGR1",4);
    put32(g->reply+4,(uint32_t)result); put64(g->reply+8,request); put64(g->reply+16,out_grant);
    g->input=0; g->output=0; g->reply_pending=true;
}
mg_memory_result mg_memory_grant_pump(mg_memory_grant *g) {
    if (!g || !g->memory || g->fd<0 || g->closed) return MG_MEMORY_INVALID;
    if (g->reply_pending) {
#ifdef __APPLE__
        ssize_t n=send(g->fd,g->reply+g->output,sizeof(g->reply)-g->output,MSG_DONTWAIT);
#else
        ssize_t n=send(g->fd,g->reply+g->output,sizeof(g->reply)-g->output,MSG_DONTWAIT|MSG_NOSIGNAL);
#endif
        if (n<0) return errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR ? MG_MEMORY_BUSY : (g->closed=true,MG_MEMORY_INVALID);
        if (!n) { g->closed=true; return MG_MEMORY_INVALID; }
        g->output+=(size_t)n;
        if (g->output==sizeof(g->reply)) { g->reply_pending=false; g->output=0; }
        return MG_MEMORY_OK;
    }
    ssize_t n=recv(g->fd,g->request+g->input,sizeof(g->request)-g->input,MSG_DONTWAIT);
    if (n<0) return errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR ? MG_MEMORY_BUSY : (g->closed=true,MG_MEMORY_INVALID);
    if (!n) { g->closed=true; return MG_MEMORY_INVALID; }
    g->wire_started=true;
    g->input+=(size_t)n; if (g->input==sizeof(g->request)) process(g);
    return MG_MEMORY_OK;
}
mg_memory_result mg_memory_grant_process_exited(mg_memory_grant *g, mg_lease_key domain, uint64_t launch_id) {
    if (!g || !g->memory) return MG_MEMORY_INVALID;
    if (domain.scope!=g->domain.scope || domain.resource!=g->domain.resource ||
        domain.generation!=g->domain.generation || launch_id!=g->launch_id) return MG_MEMORY_STALE;
    g->exit_observed=true; return settle(g);
}
