/* SPDX-License-Identifier: MIT */
#ifndef _DARWIN_C_SOURCE
#define _DARWIN_C_SOURCE
#endif
#include "sidecar.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <bsm/libbsm.h>
#include <sys/un.h>
#endif

typedef struct { pid_t pid; uid_t uid; int version; } owner_identity;

static uint64_t clock_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0 || t.tv_sec < 0) return 0;
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static ssize_t safe_send(int fd, const void *buf, size_t n) {
#ifdef __APPLE__
    return send(fd, buf, n, 0);
#else
    return send(fd, buf, n, MSG_NOSIGNAL);
#endif
}
static bool same_key(mg_lease_key a, mg_lease_key b) {
    return a.scope == b.scope && a.resource == b.resource && a.generation == b.generation;
}
static void handle(mg_supervisor *s, const uint8_t nonce[16], const mg_sidecar_request *q,
                   mg_sidecar_reply *r, uint64_t now, int *endpoint, int *event_fd,
                   int *idb_fd, int *grant_fd, int *task_fd,
                   owner_identity owner, int *incoming_fd, int *escrows) {
    memset(r, 0, sizeof(*r)); r->request_id=q->request_id; r->domain=q->domain;
    r->result=MG_WORK_INVALID;
    if (memcmp(q->nonce, nonce, 16) || !q->request_id || q->domain >= MG_GOV_DOMAINS) return;
    if ((q->operation==MG_SIDECAR_ESCROW) != (*incoming_fd>=0)) return;
    mg_governance *g=s->governance;
    r->launch_id=q->launch_id;
    r->key=g->domains[q->domain].ref.key;
    if (q->operation==MG_SIDECAR_POLICY || q->operation==MG_SIDECAR_SUBMIT ||
        q->operation==MG_SIDECAR_FINISH) {
        mg_supervised_child *c=&s->children[q->domain];
        if (!q->launch_id || !c->used || c->launch_id!=q->launch_id || c->exited) {
            r->result=MG_WORK_STALE; return;
        }
    }
    switch (q->operation) {
    case MG_SIDECAR_REGISTER:
        r->result=mg_gov_register(g,q->domain,q->key,q->quota_ns,(mg_gov_class)q->class_id,q->policy_epoch,q->cap_epoch);
        if (r->result==MG_WORK_OK) r->key=g->domains[q->domain].ref.key;
        break;
    case MG_SIDECAR_REARM: {
        if (!same_key(q->key,g->domains[q->domain].ref.key)) {
            r->result=MG_WORK_STALE; break;
        }
        mg_work_domain_ref next;
        r->result=mg_supervisor_rearm(s,q->domain,q->launch_id,&next);
        if (r->result==MG_WORK_OK) r->key=next.key;
        break;
    }
    case MG_SIDECAR_POLICY:
        if (q->quota_ns && q->quota_ns!=g->domains[q->domain].quota_ns) break;
        r->result=mg_gov_policy(g,q->domain,(mg_gov_class)q->class_id,
                                q->policy_epoch,q->cap_epoch,now);
        break;
    case MG_SIDECAR_SPAWN: {
        if (q->launch_id || !same_key(q->key,g->domains[q->domain].ref.key)) {
            r->result=MG_WORK_STALE; break;
        }
        if (q->trusted_executable != 1 || q->with_endpoint > 1 || q->with_events > 1 ||
            q->with_idb>1 || q->with_grant>1 || q->with_task>1 || q->hold_identity > 1 || q->trusted_cdp > 1 ||
            (q->with_events && !q->with_endpoint) || (q->with_idb && !q->with_events) ||
            (q->with_grant && (!q->with_endpoint || q->role==MG_SIDECAR_COOKIE_OWNER)) ||
            (q->with_task && (q->role!=MG_SIDECAR_RUNTIME || !q->with_grant)) ||
            !q->argc || q->argc > MG_SIDECAR_ARGS) break;
        if (!memchr(q->raster_socket,0,sizeof(q->raster_socket)) ||
            (q->raster_socket[0] && q->raster_socket[0] != '/')) break;
        if (q->role > MG_SIDECAR_IDB_OWNER ||
            !memchr(q->owner_path,0,sizeof(q->owner_path)) ||
            (q->role != MG_SIDECAR_RUNTIME &&
             (!q->with_endpoint || q->owner_path[0] != '/' || q->raster_socket[0] || q->trusted_cdp))) break;
        char *argv[MG_SIDECAR_ARGS+1];
        for (uint32_t i=0; i<q->argc; ++i) {
            if (!memchr(q->argv[i], 0, MG_SIDECAR_ARG_BYTES)) return;
            argv[i]=(char *)q->argv[i];
        }
        argv[q->argc]=0; pid_t pid;
        char host_pid[64], host_uid[64], host_version[64], raster[MG_SIDECAR_ARG_BYTES+32];
        char owner_path[MG_SIDECAR_ARG_BYTES+32];
        (void)snprintf(host_pid,sizeof(host_pid),"MIOA_TRUSTED_RASTER_HOST_PID=%ld",(long)owner.pid);
        (void)snprintf(host_uid,sizeof(host_uid),"MIOA_TRUSTED_RASTER_HOST_UID=%lu",(unsigned long)owner.uid);
        (void)snprintf(host_version,sizeof(host_version),"MIOA_TRUSTED_RASTER_HOST_VERSION=%d",owner.version);
        (void)snprintf(raster,sizeof(raster),"MIOA_TRUSTED_RASTER_SOCKET=%s",q->raster_socket);
        char *envp[12]; size_t e=0;
        envp[e++]=(char *)"PATH=/usr/bin:/bin";
        if (q->role == MG_SIDECAR_RUNTIME && (q->trusted_cdp || q->raster_socket[0])) {
            envp[e++]=host_pid; envp[e++]=host_uid; envp[e++]=host_version;
        }
        if (q->raster_socket[0]) envp[e++]=raster;
        if (q->with_endpoint) {
            if (q->role == MG_SIDECAR_COOKIE_OWNER) {
                envp[e++]=(char *)"MIOA_COOKIE_OWNER_FD=4";
                (void)snprintf(owner_path,sizeof(owner_path),"MIOA_COOKIE_OWNER_DB=%s",q->owner_path);
                envp[e++]=owner_path;
            } else if (q->role == MG_SIDECAR_IDB_OWNER) {
                envp[e++]=(char *)"MIOA_IDB_OWNER_FD=4";
                (void)snprintf(owner_path,sizeof(owner_path),"MIOA_IDB_OWNER_DIR=%s",q->owner_path);
                envp[e++]=owner_path;
            } else envp[e++]=(char *)"MIOA_PROFILE_SERVICE_FD=4";
        }
        if (q->with_events) envp[e++]=(char *)"MIOA_PROFILE_EVENTS_FD=5";
        if (q->with_idb) envp[e++]=(char *)"MIOA_PROFILE_IDB_FD=6";
        if (q->with_grant) envp[e++]=q->role==MG_SIDECAR_RUNTIME
            ? (char *)"MIOA_RUNTIME_GRANT_FD=7" : (char *)"MIOA_IDB_GRANT_FD=7";
        if (q->with_task) envp[e++]=(char *)"MIOA_RUNTIME_TASK_FD=8";
        envp[e]=0;
        r->result=mg_supervisor_spawn_profile(s,q->domain,argv[0],argv,envp,
                     &r->launch_id,&pid,q->with_endpoint ? endpoint : 0,
                     q->with_events ? event_fd : 0,q->with_idb ? idb_fd : 0,
                     q->with_grant ? grant_fd : 0,q->with_task ? task_fd : 0);
        if (r->result==MG_WORK_OK) {
            r->pid=pid; r->has_endpoint=q->with_endpoint != 0; r->has_events=q->with_events != 0;
            r->has_idb=q->with_idb!=0; r->has_grant=q->with_grant!=0; r->has_task=q->with_task!=0;
            if (q->raster_socket[0] || q->hold_identity || q->trusted_cdp) {
                s->children[q->domain].identity_held=true;
                s->children[q->domain].identity_deadline_ns=now+UINT64_C(5000000000);
                r->identity_held=true;
            }
        }
        break;
    }
    case MG_SIDECAR_RELEASE:
        r->result=mg_supervisor_release(s,q->domain,q->launch_id); break;
    case MG_SIDECAR_SUBMIT:
        r->result=mg_gov_submit(g,q->domain,q->task); break;
    case MG_SIDECAR_DISPATCH:
        r->result=mg_gov_next(g,now,mg_supervisor_sample_cpu,s,&r->domain,&r->slot,&r->task);
        if (r->domain<MG_GOV_DOMAINS) {
            r->launch_id=s->children[r->domain].launch_id;
            r->key=g->domains[r->domain].ref.key;
        }
        break;
    case MG_SIDECAR_STOP:
        r->result=mg_supervisor_stop(s,q->domain,q->launch_id,now); break;
    case MG_SIDECAR_FINISH: {
        if (q->slot >= MG_GOV_COMPLETIONS || !g->running[q->slot].used ||
            g->running[q->slot].domain != q->domain ||
            !q->task.id || g->running[q->slot].id != q->task.id) {
            r->result=MG_WORK_STALE; break;
        }
        uint64_t cpu;
        if (!mg_supervisor_sample_cpu(s,q->domain,&cpu) || cpu < g->running[q->slot].cpu_start_ns) {
            r->result=MG_WORK_BUDGET; break;
        }
        r->cpu_ns=cpu-g->running[q->slot].cpu_start_ns;
        r->result=mg_gov_finish(g,q->slot,now,r->cpu_ns); break;
    }
    case MG_SIDECAR_QUERY: {
        mg_supervised_child *c=&s->children[q->domain];
        if (!c->used || c->launch_id != q->launch_id) { r->result=MG_WORK_STALE; break; }
        r->result=MG_WORK_OK; r->launch_id=c->launch_id; r->pid=c->pid; r->status=c->status;
        r->exited=c->exited; r->stopping=c->stopping; r->cpu_available=c->cpu_available; r->cpu_ns=c->last_cpu_ns;
        r->exit_pending=c->exit_pending; r->identity_held=c->identity_held; r->automatic_stop=c->automatic_stop;
        break;
    }
    case MG_SIDECAR_IDENTITY_ACK: {
        mg_supervised_child *c=&s->children[q->domain];
        siginfo_t info; memset(&info,0,sizeof(info));
        if (!c->used || c->launch_id!=q->launch_id || !c->identity_held ||
            now>=c->identity_deadline_ns || waitid(P_PID,(id_t)c->pid,&info,WEXITED|WNOHANG|WNOWAIT)<0 ||
            info.si_pid==c->pid) { r->result=MG_WORK_STALE; break; }
        c->identity_held=false; r->result=MG_WORK_OK; r->pid=c->pid; r->launch_id=c->launch_id;
        break;
    }
    case MG_SIDECAR_ESCROW:
        if (escrows[q->domain]>=0) { r->result=MG_WORK_BUSY; break; }
        escrows[q->domain]=*incoming_fd; *incoming_fd=-1; r->result=MG_WORK_OK; break;
    case MG_SIDECAR_ESCROW_RELEASE:
        if (escrows[q->domain]<0) { r->result=MG_WORK_STALE; break; }
        for (uint32_t i=0;i<MG_GOV_DOMAINS;++i)
            if (s->children[i].used && !s->children[i].exited) { r->result=MG_WORK_BUSY; return; }
        close(escrows[q->domain]); escrows[q->domain]=-1; r->result=MG_WORK_OK; break;
    default: break;
    }
}
static void close_channels(int *endpoint, int *events, int *idb, int *grant, int *task) {
    int *channels[]={endpoint,events,idb,grant,task};
    for (size_t i=0;i<sizeof(channels)/sizeof(channels[0]);++i)
        if (*channels[i]>=0) { close(*channels[i]); *channels[i]=-1; }
}
static void child_loop(int fd, uint32_t concurrency, const uint8_t nonce[16], owner_identity owner) {
    mg_governance g; mg_supervisor s; uint64_t now=clock_ns();
    if (!now || mg_gov_init(&g,concurrency,now,(mg_lease_key){UINT64_MAX,UINT64_MAX,1}) != MG_WORK_OK) _exit(126);
    mg_supervisor_init(&s,&g);
    mg_sidecar_request q; mg_sidecar_reply r; size_t input=0, output=0;
    bool have_reply=false, disconnected=false, rights_sent=false;
    uint64_t last_request=0;
    int pending_fd=-1, pending_event=-1, pending_idb=-1, pending_grant=-1, pending_task=-1;
    int incoming_fd=-1, escrows[MG_GOV_DOMAINS];
    for (uint32_t i=0;i<MG_GOV_DOMAINS;++i) escrows[i]=-1;
    uint64_t next=now;
    for (;;) {
        now=clock_ns(); if (!now) _exit(126);
        if (now >= next) { (void)mg_supervisor_poll(&s,now); next=now+MG_GOV_PERIOD_NS; }
        if (disconnected) {
            bool pending=false;
            for (uint32_t i=0; i<MG_GOV_DOMAINS; ++i) {
                mg_supervised_child *c=&s.children[i];
                if (!c->used || c->exited) continue;
                pending=true; c->identity_held=false; (void)mg_supervisor_stop(&s,i,c->launch_id,now);
            }
            if (!pending) _exit(0);
        }
        struct pollfd p={.fd=disconnected ? -1 : fd,
                         .events=(short)(have_reply ? POLLOUT : POLLIN)};
        uint64_t remaining = next > now ? next - now : 0;
        int timeout=(int)(remaining / UINT64_C(1000000) + (remaining % UINT64_C(1000000) != 0));
        if (timeout > 100) timeout=100;
        int count=poll(&p,1,timeout);
        if (count < 0) { if (errno==EINTR) continue; disconnected=true; continue; }
        if (p.revents & (POLLERR|POLLHUP|POLLNVAL)) disconnected=true;
        if (!disconnected && (p.revents & POLLIN) && !have_reply) {
            int received=-1; size_t received_count=1;
            ssize_t n=mg_sidecar_recv_fds(fd,(char *)&q+input,sizeof(q)-input,
                                          &received,&received_count);
            if (n>0 && received_count) {
                if (incoming_fd>=0) { close(received); disconnected=true; }
                else incoming_fd=received;
            }
            if (!n) disconnected=true;
            else if (n<0) { if (errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) disconnected=true; }
            else if (!disconnected && (input+=(size_t)n)==sizeof(q)) {
                if (q.operation==MG_SIDECAR_CHANNELS_ACK) {
                    if (incoming_fd>=0 || !rights_sent || pending_fd<0 ||
                        memcmp(q.nonce,nonce,16) || q.request_id!=last_request ||
                        q.domain!=r.domain || q.launch_id!=r.launch_id) disconnected=true;
                    else {
                        close_channels(&pending_fd,&pending_event,&pending_idb,&pending_grant,&pending_task);
                        rights_sent=false;
                    }
                } else if (pending_fd>=0) {
                    /* No new request can overtake receipt of the prior FDs. */
                    disconnected=true;
                } else {
                    if (q.request_id <= last_request || memcmp(q.nonce,nonce,16)) {
                        memset(&r,0,sizeof(r)); r.request_id=q.request_id; r.result=MG_WORK_STALE;
                    } else {
                        last_request=q.request_id; handle(&s,nonce,&q,&r,now,&pending_fd,&pending_event,
                            &pending_idb,&pending_grant,&pending_task,owner,&incoming_fd,escrows);
                    }
                    output=0; have_reply=true;
                }
                if (incoming_fd>=0) { close(incoming_fd); incoming_fd=-1; }
                input=0;
            }
        }
        if (!disconnected && have_reply && (p.revents & POLLOUT)) {
            ssize_t n;
            if (pending_fd >= 0 && !rights_sent) {
                int fds[5]; size_t count=0; fds[count++]=pending_fd;
                if (pending_event>=0) fds[count++]=pending_event;
                if (pending_idb>=0) fds[count++]=pending_idb;
                if (pending_grant>=0) fds[count++]=pending_grant;
                if (pending_task>=0) fds[count++]=pending_task;
                n=mg_sidecar_send_fds(fd,(char *)&r+output,sizeof(r)-output,fds,count);
                /* Darwin can mark the received socket EOF if the sender's last
                 * copy closes before recvmsg. Send once; retain until receipt. */
                if (n>0) rights_sent=true;
            } else n=safe_send(fd,(char *)&r+output,sizeof(r)-output);
            if (n<0) { if (errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) disconnected=true; }
            else if (!n) disconnected=true;
            else if ((output+=(size_t)n)==sizeof(r)) have_reply=false;
        }
        if (disconnected)
            close_channels(&pending_fd,&pending_event,&pending_idb,&pending_grant,&pending_task);
        if (disconnected && incoming_fd>=0) { close(incoming_fd); incoming_fd=-1; }
    }
}
mg_work_result mg_sidecar_start(mg_sidecar_client *c, uint32_t concurrency) {
    if (!c || !concurrency || concurrency>4) return MG_WORK_INVALID;
    int fd_ceiling;
    if (!mg_supervisor_fd_ceiling(&fd_ceiling)) return MG_WORK_BUDGET;
    uint8_t nonce[16]; if (getentropy(nonce,sizeof(nonce)) != 0) return MG_WORK_BUDGET;
    int fds[2]; if (socketpair(AF_UNIX,SOCK_STREAM,0,fds)!=0) return MG_WORK_FULL;
    owner_identity owner={0};
#ifdef __APPLE__
    audit_token_t token; socklen_t token_len=sizeof(token);
    if (getsockopt(fds[1],SOL_LOCAL,LOCAL_PEERTOKEN,&token,&token_len)!=0 || token_len!=sizeof(token)) {
        close(fds[0]); close(fds[1]); return MG_WORK_BUDGET;
    }
    owner.pid=audit_token_to_pid(token); owner.uid=audit_token_to_euid(token);
    owner.version=audit_token_to_pidversion(token);
    if (owner.pid!=getpid() || owner.uid!=geteuid() || owner.version<0) {
        close(fds[0]); close(fds[1]); return MG_WORK_BUDGET;
    }
#else
    close(fds[0]); close(fds[1]); return MG_WORK_BUDGET;
#endif
#ifdef __APPLE__
    int on=1;
    if (setsockopt(fds[0],SOL_SOCKET,SO_NOSIGPIPE,&on,sizeof(on)) ||
        setsockopt(fds[1],SOL_SOCKET,SO_NOSIGPIPE,&on,sizeof(on))) {
        close(fds[0]); close(fds[1]); return MG_WORK_FULL;
    }
#endif
    if (fcntl(fds[0],F_SETFD,FD_CLOEXEC)<0 || fcntl(fds[1],F_SETFD,FD_CLOEXEC)<0 ||
        fcntl(fds[0],F_SETFL,O_NONBLOCK)<0 || fcntl(fds[1],F_SETFL,O_NONBLOCK)<0) {
        close(fds[0]); close(fds[1]); return MG_WORK_FULL;
    }
    pid_t pid=fork();
    if (pid<0) { close(fds[0]); close(fds[1]); return MG_WORK_FULL; }
    if (!pid) {
        close(fds[0]);
        if (fds[1]!=3) { if (dup2(fds[1],3)<0) _exit(126); close(fds[1]); }
        if (!mg_supervisor_close_extra(4)) _exit(126);
        child_loop(3,concurrency,nonce,owner); _exit(126);
    }
    close(fds[1]); memset(c,0,sizeof(*c)); c->fd=fds[0]; c->tx_fd=-1;
    c->received_fd=c->received_event_fd=c->received_idb_fd=c->received_grant_fd=c->received_task_fd=-1;
    c->pid=pid; memcpy(c->nonce,nonce,16);
    return MG_WORK_OK;
}
mg_work_result mg_sidecar_send(mg_sidecar_client *c, const mg_sidecar_request *q) {
    if (!c || !q || c->fd<0 || !q->request_id) return MG_WORK_INVALID;
    if (c->awaiting_reply || c->received_fd>=0 || c->received_event_fd>=0 ||
        c->received_idb_fd>=0 || c->received_grant_fd>=0 || c->received_task_fd>=0) return MG_WORK_BUSY;
    c->tx=*q; memcpy(c->tx.nonce,c->nonce,16); c->tx_offset=0;
    c->tx_pending=true; c->awaiting_reply=true;
    return MG_WORK_OK;
}
mg_work_result mg_sidecar_send_lease(mg_sidecar_client *c, const mg_sidecar_request *q, int fd) {
    if (!c || !q || fd<0 || q->operation!=MG_SIDECAR_ESCROW) return MG_WORK_INVALID;
    int copy=fcntl(fd,F_DUPFD_CLOEXEC,0); if (copy<0) return MG_WORK_FULL;
    mg_work_result r=mg_sidecar_send(c,q);
    if (r!=MG_WORK_OK) { close(copy); return r; }
    c->tx_fd=copy; return MG_WORK_OK;
}
static mg_work_result received_reply(mg_sidecar_client *c, mg_sidecar_reply *out) {
    uint32_t pos=0;
    if (c->rx.has_endpoint) c->received_fd=c->received_raw[pos++];
    if (c->rx.has_events) c->received_event_fd=c->received_raw[pos++];
    if (c->rx.has_idb) c->received_idb_fd=c->received_raw[pos++];
    if (c->rx.has_grant) c->received_grant_fd=c->received_raw[pos++];
    if (c->rx.has_task) c->received_task_fd=c->received_raw[pos++];
    c->received_count=0; c->acknowledging_channels=false;
    *out=c->rx; c->rx_offset=0; c->awaiting_reply=false; return MG_WORK_OK;
}
static mg_work_result receive_failure(mg_sidecar_client *c, mg_work_result result) {
    if (c->tx_fd>=0) { close(c->tx_fd); c->tx_fd=-1; }
    for (uint32_t i=0;i<c->received_count;++i) close(c->received_raw[i]);
    c->received_count=0;
    return result;
}
mg_work_result mg_sidecar_receive(mg_sidecar_client *c, mg_sidecar_reply *out) {
    if (!c || !out || c->fd<0) return MG_WORK_INVALID;
    if (!c->awaiting_reply) return MG_WORK_INVALID;
    if (c->tx_pending) {
        ssize_t n=c->tx_fd>=0 && !c->tx_offset
            ? mg_sidecar_send_fds(c->fd,(char *)&c->tx+c->tx_offset,sizeof(c->tx)-c->tx_offset,&c->tx_fd,1)
            : safe_send(c->fd,(char *)&c->tx+c->tx_offset,sizeof(c->tx)-c->tx_offset);
        if (n<0 && (errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR)) return MG_WORK_BUSY;
        if (n<=0) return receive_failure(c,MG_WORK_CLOSED);
        c->tx_offset+=(size_t)n;
        if (c->tx_offset<sizeof(c->tx)) return MG_WORK_BUSY;
        c->tx_pending=false;
    }
    if (c->acknowledging_channels) return received_reply(c,out);
    int received[5]; size_t count=5;
    ssize_t n=mg_sidecar_recv_fds(c->fd,(char *)&c->rx+c->rx_offset,
                                  sizeof(c->rx)-c->rx_offset,received,&count);
    if (n>0 && count) {
        if (c->received_count) {
            for (size_t i=0;i<count;++i) close(received[i]);
            return receive_failure(c,MG_WORK_INVALID);
        }
        memcpy(c->received_raw,received,count*sizeof(int));
        c->received_count=(uint32_t)count;
    }
    if (n<0 && (errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR)) return MG_WORK_BUSY;
    if (n<=0) return receive_failure(c,MG_WORK_CLOSED);
    c->rx_offset+=(size_t)n;
    if (c->rx_offset<sizeof(c->rx)) return MG_WORK_BUSY;
    if (c->rx.request_id != c->tx.request_id) return receive_failure(c,MG_WORK_STALE);
    uint32_t expected=(uint32_t)c->rx.has_endpoint+(uint32_t)c->rx.has_events+
                      (uint32_t)c->rx.has_idb+(uint32_t)c->rx.has_grant+(uint32_t)c->rx.has_task;
    if (expected!=c->received_count) return receive_failure(c,MG_WORK_INVALID);
    /* ESCROW also retains its sending copy through the receiver's reply. */
    if (c->tx_fd>=0) { close(c->tx_fd); c->tx_fd=-1; }
    if (expected) {
        memset(&c->tx,0,sizeof(c->tx)); memcpy(c->tx.nonce,c->nonce,16);
        c->tx.operation=MG_SIDECAR_CHANNELS_ACK; c->tx.request_id=c->rx.request_id;
        c->tx.domain=c->rx.domain; c->tx.launch_id=c->rx.launch_id;
        c->tx_offset=0; c->tx_pending=true; c->acknowledging_channels=true;
        return MG_WORK_BUSY;
    }
    return received_reply(c,out);
}
mg_work_result mg_sidecar_verify_peer(int fd, pid_t expected_pid, uid_t expected_uid) {
    if (fd<0 || expected_pid<=0) return MG_WORK_INVALID;
#ifdef __APPLE__
    audit_token_t token; socklen_t size=sizeof(token);
    if (getsockopt(fd,SOL_LOCAL,LOCAL_PEERTOKEN,&token,&size)!=0 || size!=sizeof(token))
        return MG_WORK_BUDGET;
    return audit_token_to_pid(token)==expected_pid && audit_token_to_euid(token)==expected_uid
        ? MG_WORK_OK : MG_WORK_STALE;
#else
    (void)expected_uid; return MG_WORK_BUDGET;
#endif
}
mg_work_result mg_sidecar_verify_peer_version(int fd, pid_t expected_pid, uid_t expected_uid,
                                              int expected_version) {
    if (fd<0 || expected_pid<=0 || expected_version<0) return MG_WORK_INVALID;
#ifdef __APPLE__
    audit_token_t token; socklen_t size=sizeof(token);
    if (getsockopt(fd,SOL_LOCAL,LOCAL_PEERTOKEN,&token,&size)!=0 || size!=sizeof(token))
        return MG_WORK_BUDGET;
    return audit_token_to_pid(token)==expected_pid && audit_token_to_euid(token)==expected_uid &&
           audit_token_to_pidversion(token)==expected_version ? MG_WORK_OK : MG_WORK_STALE;
#else
    (void)expected_uid; return MG_WORK_BUDGET;
#endif
}
ssize_t mg_sidecar_send_fds(int fd, const void *bytes, size_t length,
                            const int *fds, size_t count) {
    if (fd<0 || !bytes || !length || count>8 || (count && !fds)) {
        errno=EINVAL; return -1;
    }
    union { struct cmsghdr align; char data[CMSG_SPACE(8*sizeof(int))]; } control;
    struct iovec iov={.iov_base=(void *)bytes,.iov_len=length};
    struct msghdr msg={0}; msg.msg_iov=&iov; msg.msg_iovlen=1;
    if (count) {
        memset(&control,0,sizeof(control));
        msg.msg_control=control.data; msg.msg_controllen=CMSG_SPACE(count*sizeof(int));
        struct cmsghdr *cm=CMSG_FIRSTHDR(&msg);
        cm->cmsg_level=SOL_SOCKET; cm->cmsg_type=SCM_RIGHTS; cm->cmsg_len=CMSG_LEN(count*sizeof(int));
        memcpy(CMSG_DATA(cm),fds,count*sizeof(int));
    }
#ifdef __APPLE__
    int enabled=1;
    if (setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&enabled,sizeof(enabled))<0) return -1;
    return sendmsg(fd,&msg,MSG_DONTWAIT);
#else
    return sendmsg(fd,&msg,MSG_DONTWAIT|MSG_NOSIGNAL);
#endif
}
ssize_t mg_sidecar_recv_fds(int fd, void *bytes, size_t length, int *fds, size_t *count) {
    if (fd<0 || !bytes || !length || !count || *count>8 || (*count && !fds)) {
        errno=EINVAL; return -1;
    }
    size_t capacity=*count, received=0; *count=0;
    /* Receive the XNU rights ceiling before enforcing the protocol limit:
     * truncated Darwin rights may be installed without revealing their FDs. */
    int local[8]; union { struct cmsghdr align; char data[CMSG_SPACE(512*sizeof(int))]; } control;
    memset(&control,0,sizeof(control));
    struct iovec iov={.iov_base=bytes,.iov_len=length};
    struct msghdr msg={0}; msg.msg_iov=&iov; msg.msg_iovlen=1;
    msg.msg_control=control.data; msg.msg_controllen=sizeof(control.data);
    ssize_t n=recvmsg(fd,&msg,MSG_DONTWAIT);
    if (n<=0) return n;
    bool invalid=(msg.msg_flags & MSG_CTRUNC)!=0;
    for (struct cmsghdr *cm=CMSG_FIRSTHDR(&msg); cm; cm=CMSG_NXTHDR(&msg,cm)) {
        size_t available=msg.msg_controllen-(size_t)((unsigned char *)cm-(unsigned char *)msg.msg_control);
        size_t length=cm->cmsg_len;
        bool truncated=length>available;
        if (truncated) { length=available; invalid=true; }
        if (cm->cmsg_level!=SOL_SOCKET || cm->cmsg_type!=SCM_RIGHTS ||
            length<CMSG_LEN(0)) { invalid=true; if (truncated) break; continue; }
        size_t bytes=length-CMSG_LEN(0);
        if (bytes%sizeof(int)) invalid=true;
        size_t entries=bytes/sizeof(int);
        const unsigned char *data=CMSG_DATA(cm);
        for (size_t i=0;i<entries;++i) {
            int item; memcpy(&item,data+i*sizeof(int),sizeof(item));
            if (received<8) local[received++]=item;
            else { close(item); invalid=true; }
        }
        if (truncated) break;
    }
    if (received>capacity) invalid=true;
    if (!invalid) for (size_t i=0;i<received;++i)
        if (fcntl(local[i],F_SETFD,FD_CLOEXEC)<0) { invalid=true; break; }
    if (invalid) {
        for (size_t i=0;i<received;++i) close(local[i]);
        errno=EMSGSIZE; return -1;
    }
    for (size_t i=0;i<received;++i) fds[i]=local[i];
    *count=received; return n;
}
