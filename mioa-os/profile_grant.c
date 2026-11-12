/* SPDX-License-Identifier: MIT */
#include "profile_grant.h"
#include <errno.h>
#include <string.h>
#include <sys/socket.h>

/* PGR1: request magic, op:u32, sequence:u64, ticket:u64, host:u64,
 * handles:u64, logical_disk:u64; response magic, result:u32, sequence:u64,
 * ticket:u64, zero:u64. Big endian. The socket, not message data, identifies
 * the payer. A release asserts disposal by the trusted Profile service. */
static uint64_t get64(const unsigned char *p) { uint64_t v=0; for (unsigned i=0;i<8;++i) v=(v<<8)|p[i]; return v; }
static uint32_t get32(const unsigned char *p) { uint32_t v=0; for (unsigned i=0;i<4;++i) v=(v<<8)|p[i]; return v; }
static void put64(unsigned char *p,uint64_t v) { for (int i=7;i>=0;--i) { p[i]=(unsigned char)v; v>>=8; } }
static void put32(unsigned char *p,uint32_t v) { for (int i=3;i>=0;--i) { p[i]=(unsigned char)v; v>>=8; } }

mg_memory_result mg_profile_grant_init(mg_profile_grant *g, mg_memory *m, int fd,
                                       mg_lease_key account, mg_lease_key resource) {
    if (!g || !m || fd<0 || !account.scope || !account.resource || !account.generation ||
        !resource.scope || !resource.resource || !resource.generation) return MG_MEMORY_INVALID;
#ifdef __APPLE__
    int enabled=1;
    if (setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&enabled,sizeof(enabled))<0) return MG_MEMORY_INVALID;
#endif
    memset(g,0,sizeof(*g)); g->memory=m; g->fd=fd; g->account=account; g->resource=resource;
    return MG_MEMORY_OK;
}
static void process(mg_profile_grant *g) {
    const unsigned char *q=g->request;
    uint32_t op=get32(q+4); uint64_t seq=get64(q+8), token=get64(q+16);
    uint64_t host=get64(q+24), handles=get64(q+32), disk=get64(q+40), result_token=0;
    mg_memory_result result=MG_MEMORY_INVALID;
    if (!memcmp(q,"PGR1",4) && seq && seq>g->last_request) {
        g->last_request=seq;
        if (op==1 && !token && (host || handles || disk)) {
            unsigned i=0; while (i<MG_PROFILE_GRANT_TICKETS && g->live[i]) ++i;
            if (i==MG_PROFILE_GRANT_TICKETS) result=MG_MEMORY_FULL;
            else {
                mg_memory_amount amount={{0}}; amount.value[MG_MEMORY_HOST_BYTES]=host;
                amount.value[MG_MEMORY_HANDLES]=handles; amount.value[MG_MEMORY_DISK_BYTES]=disk;
                mg_memory_ticket t;
                result=mg_memory_reserve(g->memory,g->account,g->resource,amount,MG_MEMORY_ORDINARY,&t);
                if (result==MG_MEMORY_OK) {
                    result=mg_memory_commit(g->memory,t,amount);
                    if (result==MG_MEMORY_OK) { g->tickets[i]=t; g->live[i]=true; result_token=t.serial; }
                    else (void)mg_memory_cancel(g->memory,t);
                }
            }
        } else if (op==2 && token && !host && !handles && !disk) {
            unsigned i=0; while (i<MG_PROFILE_GRANT_TICKETS && (!g->live[i] || g->tickets[i].serial!=token)) ++i;
            if (i<MG_PROFILE_GRANT_TICKETS) {
                result=mg_memory_retire(g->memory,g->tickets[i]);
                if (result==MG_MEMORY_OK) result=mg_memory_release(g->memory,g->tickets[i]);
                if (result==MG_MEMORY_OK) { g->live[i]=false; result_token=token; }
            }
        }
    }
    memset(g->reply,0,sizeof(g->reply)); memcpy(g->reply,"PGR1",4);
    put32(g->reply+4,(uint32_t)result); put64(g->reply+8,seq); put64(g->reply+16,result_token);
    g->input=0; g->output=0; g->reply_pending=true;
}
mg_memory_result mg_profile_grant_pump(mg_profile_grant *g) {
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
    g->input+=(size_t)n; if (g->input==sizeof(g->request)) process(g);
    return MG_MEMORY_OK;
}
