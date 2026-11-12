/* SPDX-License-Identifier: MIT */
#ifndef _DARWIN_C_SOURCE
#define _DARWIN_C_SOURCE
#endif
#include "supervisor.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <libproc.h>
#include <mach/mach_time.h>
#endif

void mg_supervisor_init(mg_supervisor *s, mg_governance *g) {
    memset(s, 0, sizeof(*s)); s->governance = g; s->next_launch_id = 1;
#ifdef __APPLE__
    mach_timebase_info_data_t timebase;
    if (mach_timebase_info(&timebase)==KERN_SUCCESS && timebase.numer && timebase.denom) {
        s->cpu_timebase_numer=timebase.numer; s->cpu_timebase_denom=timebase.denom;
    }
#endif
    for (uint32_t i=0; i<MG_GOV_DOMAINS; ++i) s->children[i].gate_fd = -1;
}
bool mg_supervisor_fd_ceiling(int *ceiling) {
    struct rlimit limit;
    if (!ceiling || getrlimit(RLIMIT_NOFILE, &limit) != 0 ||
        limit.rlim_cur == RLIM_INFINITY || limit.rlim_cur > INT_MAX) return false;
    *ceiling = (int)limit.rlim_cur;
    return *ceiling >= 5;
}
bool mg_supervisor_close_extra(int first) {
#ifdef __APPLE__
    struct proc_fdinfo entries[128];
    for (;;) {
        int bytes=proc_pidinfo(getpid(),PROC_PIDLISTFDS,0,entries,(int)sizeof(entries));
        if (bytes<=0 || bytes % (int)sizeof(entries[0]) != 0) return false;
        int count=bytes/(int)sizeof(entries[0]), closed=0;
        for (int i=0; i<count; ++i) if (entries[i].proc_fd >= first) {
            int rc;
            do { rc=close(entries[i].proc_fd); } while (rc<0 && errno==EINTR);
            if (rc<0 && errno!=EBADF) return false;
            ++closed;
        }
        if (bytes < (int)sizeof(entries)) return true;
        if (!closed) return false;
    }
#else
    (void)first; return false;
#endif
}
static mg_work_result spawn_child(mg_supervisor *s, uint32_t domain,
    const char *exe, char *const argv[], char *const envp[], uint64_t *launch,
    pid_t *pid, int *endpoint_fd, int *event_fd, int *idb_fd, int *grant_fd, int *task_fd) {
    if (!s || !s->governance || domain >= MG_GOV_DOMAINS || !exe || exe[0] != '/' ||
        !argv || !argv[0] || !launch || !pid || !s->governance->domains[domain].registered)
        return MG_WORK_INVALID;
    if (s->children[domain].used) return MG_WORK_BUSY;
    if (s->governance->domains[domain].stopping) return MG_WORK_CLOSED;
    if (!s->next_launch_id) return MG_WORK_EXHAUSTED;
    int fd_ceiling;
    if (!mg_supervisor_fd_ceiling(&fd_ceiling)) return MG_WORK_BUDGET;
    int gate[2]={-1,-1}, endpoint[2]={-1,-1}, events[2]={-1,-1};
    int idb[2]={-1,-1}, grant[2]={-1,-1}, task[2]={-1,-1};
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, gate) != 0) goto failed;
    if (fcntl(gate[0], F_SETFD, FD_CLOEXEC) < 0 ||
        fcntl(gate[1], F_SETFD, FD_CLOEXEC) < 0) goto failed;
#ifdef __APPLE__
    int no_sigpipe = 1;
    if (setsockopt(gate[1], SOL_SOCKET, SO_NOSIGPIPE, &no_sigpipe, sizeof(no_sigpipe)) != 0)
        goto failed;
#endif
    if (endpoint_fd && (socketpair(AF_UNIX, SOCK_STREAM, 0, endpoint) != 0 ||
        fcntl(endpoint[0], F_SETFD, FD_CLOEXEC) < 0 ||
        fcntl(endpoint[1], F_SETFD, FD_CLOEXEC) < 0)) goto failed;
    if (event_fd && (!endpoint_fd || socketpair(AF_UNIX, SOCK_STREAM, 0, events) != 0 ||
        fcntl(events[0], F_SETFD, FD_CLOEXEC) < 0 ||
        fcntl(events[1], F_SETFD, FD_CLOEXEC) < 0)) goto failed;
    if ((idb_fd && (!event_fd || socketpair(AF_UNIX,SOCK_STREAM,0,idb)!=0 ||
         fcntl(idb[0],F_SETFD,FD_CLOEXEC)<0 || fcntl(idb[1],F_SETFD,FD_CLOEXEC)<0)) ||
        (grant_fd && (!endpoint_fd || socketpair(AF_UNIX,SOCK_STREAM,0,grant)!=0 ||
         fcntl(grant[0],F_SETFD,FD_CLOEXEC)<0 || fcntl(grant[1],F_SETFD,FD_CLOEXEC)<0)) ||
        (task_fd && (!grant_fd || socketpair(AF_UNIX,SOCK_STREAM,0,task)!=0 ||
         fcntl(task[0],F_SETFD,FD_CLOEXEC)<0 || fcntl(task[1],F_SETFD,FD_CLOEXEC)<0))) goto failed;
    pid_t child = fork();
    if (child < 0) goto failed;
    if (!child) {
        close(gate[1]);
        if (endpoint_fd) close(endpoint[0]);
        if (event_fd) close(events[0]);
        if (idb_fd) close(idb[0]);
        if (grant_fd) close(grant[0]);
        if (task_fd) close(task[0]);
        /* Copy all sources above the fixed target range before dup2, so no
         * descriptor-number collision can replace a different channel. */
        int gate_copy=fcntl(gate[0], F_DUPFD_CLOEXEC, 9);
        int endpoint_copy=endpoint_fd ? fcntl(endpoint[1], F_DUPFD_CLOEXEC, 9) : -1;
        int event_copy=event_fd ? fcntl(events[1], F_DUPFD_CLOEXEC, 9) : -1;
        int idb_copy=idb_fd ? fcntl(idb[1], F_DUPFD_CLOEXEC, 9) : -1;
        int grant_copy=grant_fd ? fcntl(grant[1], F_DUPFD_CLOEXEC, 9) : -1;
        int task_copy=task_fd ? fcntl(task[1], F_DUPFD_CLOEXEC, 9) : -1;
        if (gate_copy<0 || (endpoint_fd && endpoint_copy<0) || (event_fd && event_copy<0) ||
            (idb_fd && idb_copy<0) || (grant_fd && grant_copy<0) || (task_fd && task_copy<0)) _exit(126);
        if (dup2(gate_copy,3)<0 || (endpoint_fd && dup2(endpoint_copy,4)<0) ||
            (event_fd && dup2(event_copy,5)<0) || (idb_fd && dup2(idb_copy,6)<0) ||
            (grant_fd && dup2(grant_copy,7)<0) || (task_fd && dup2(task_copy,8)<0)) _exit(126);
        if ((endpoint_fd && fcntl(4,F_SETFD,0)<0) || (event_fd && fcntl(5,F_SETFD,0)<0) ||
            (idb_fd && fcntl(6,F_SETFD,0)<0) || (grant_fd && fcntl(7,F_SETFD,0)<0) ||
            (task_fd && fcntl(8,F_SETFD,0)<0)) _exit(126);
        if (!endpoint_fd) close(4);
        if (!event_fd) close(5);
        if (!idb_fd) close(6);
        if (!grant_fd) close(7);
        if (!task_fd) close(8);
        if (!mg_supervisor_close_extra(9)) _exit(126);
        char token; ssize_t n;
        do { n = read(3, &token, 1); } while (n < 0 && errno == EINTR);
        close(3);
        if (n != 1 || token != 'R') _exit(125);
        if (envp) execve(exe, argv, envp); else execv(exe, argv);
        _exit(127);
    }
    close(gate[0]);
    if (endpoint_fd) { close(endpoint[1]); *endpoint_fd = endpoint[0]; }
    if (event_fd) { close(events[1]); *event_fd=events[0]; }
    if (idb_fd) { close(idb[1]); *idb_fd=idb[0]; }
    if (grant_fd) { close(grant[1]); *grant_fd=grant[0]; }
    if (task_fd) { close(task[1]); *task_fd=task[0]; }
    uint64_t id = s->next_launch_id;
    s->next_launch_id = id == UINT64_MAX ? 0 : id + 1;
    s->children[domain] = (mg_supervised_child){.pid=child, .gate_fd=gate[1], .launch_id=id, .used=true};
    *launch = id; *pid = child; return MG_WORK_OK;
failed:
    for (int i=0; i<2; ++i) {
        if (gate[i]>=0) close(gate[i]);
        if (endpoint[i]>=0) close(endpoint[i]);
        if (events[i]>=0) close(events[i]);
        if (idb[i]>=0) close(idb[i]);
        if (grant[i]>=0) close(grant[i]);
        if (task[i]>=0) close(task[i]);
    }
    return MG_WORK_FULL;
}
mg_work_result mg_supervisor_spawn(mg_supervisor *s, uint32_t domain,
    const char *exe, char *const argv[], uint64_t *launch, pid_t *pid) {
    return spawn_child(s,domain,exe,argv,0,launch,pid,0,0,0,0,0);
}
mg_work_result mg_supervisor_spawn_endpoint(mg_supervisor *s, uint32_t domain,
    const char *exe, char *const argv[], char *const envp[], uint64_t *launch,
    pid_t *pid, int *endpoint_fd) {
    return spawn_child(s,domain,exe,argv,envp,launch,pid,endpoint_fd,0,0,0,0);
}
mg_work_result mg_supervisor_spawn_channels(mg_supervisor *s, uint32_t domain,
    const char *exe, char *const argv[], char *const envp[], uint64_t *launch,
    pid_t *pid, int *endpoint_fd, int *event_fd) {
    return spawn_child(s,domain,exe,argv,envp,launch,pid,endpoint_fd,event_fd,0,0,0);
}
mg_work_result mg_supervisor_spawn_profile(mg_supervisor *s, uint32_t domain,
    const char *exe, char *const argv[], char *const envp[], uint64_t *launch,
    pid_t *pid, int *endpoint_fd, int *event_fd, int *idb_fd, int *grant_fd, int *task_fd) {
    return spawn_child(s,domain,exe,argv,envp,launch,pid,endpoint_fd,event_fd,idb_fd,grant_fd,task_fd);
}
mg_work_result mg_supervisor_release(mg_supervisor *s, uint32_t domain, uint64_t id) {
    if (!s || domain >= MG_GOV_DOMAINS || !id) return MG_WORK_INVALID;
    mg_supervised_child *c = &s->children[domain];
    if (!c->used || c->launch_id != id || c->exited) return MG_WORK_STALE;
    if (c->stopping || s->governance->domains[domain].stopping) return MG_WORK_CLOSED;
    if (c->released || c->gate_fd < 0) return MG_WORK_BUSY;
#ifdef __APPLE__
    ssize_t n; do { n = send(c->gate_fd, "R", 1, 0); } while (n < 0 && errno == EINTR);
#else
    ssize_t n; do { n = send(c->gate_fd, "R", 1, MSG_NOSIGNAL); } while (n < 0 && errno == EINTR);
#endif
    close(c->gate_fd); c->gate_fd=-1;
    if (n != 1) return MG_WORK_CLOSED;
    c->released = true; return MG_WORK_OK;
}
mg_work_result mg_supervisor_stop(mg_supervisor *s, uint32_t domain, uint64_t id, uint64_t now) {
    if (!s || domain >= MG_GOV_DOMAINS || !id || now < s->now_ns) return MG_WORK_INVALID;
    mg_supervised_child *c = &s->children[domain];
    if (!c->used || c->launch_id != id) return MG_WORK_STALE;
    if (!c->stopping) {
        mg_work_result r = mg_gov_stop(s->governance, domain); if (r != MG_WORK_OK) return r;
        c->stopping = true; c->stop_ns = now;
        if (c->gate_fd >= 0) { close(c->gate_fd); c->gate_fd=-1; }
    }
    s->now_ns = now;
    return MG_WORK_OK;
}
mg_work_result mg_supervisor_rearm(mg_supervisor *s, uint32_t domain, uint64_t id,
                                    mg_work_domain_ref *next) {
    if (!s || !s->governance || !next || domain >= MG_GOV_DOMAINS || !id ||
        (domain > 0 && domain < 4)) return MG_WORK_INVALID;
    mg_supervised_child *c = &s->children[domain];
    if (!c->used || c->launch_id != id) return MG_WORK_STALE;
    if (!c->stopping || !c->exited || c->identity_held) return MG_WORK_BUSY;
    mg_work_result r = mg_gov_rearm(s->governance, domain, next);
    if (r != MG_WORK_OK) return r;
    /* The prior launch is a tombstone until a successful fork replaces it. */
    c->used = false;
    return MG_WORK_OK;
}
static void automatic_stop(void *ctx, uint32_t domain) {
    mg_supervisor *s = ctx; mg_supervised_child *c = &s->children[domain];
    if (!c->stopping) { c->stopping = true; c->automatic_stop = true; c->stop_ns = s->now_ns;
        if (c->gate_fd >= 0) { close(c->gate_fd); c->gate_fd = -1; }
    }
}
static bool process_cpu(const mg_supervisor *s, pid_t pid, uint64_t *cpu) {
#ifdef __APPLE__
    struct rusage_info_v2 info;
    if (!s->cpu_timebase_denom ||
        proc_pid_rusage(pid, RUSAGE_INFO_V2, (rusage_info_t *)&info) != 0 ||
        info.ri_user_time > UINT64_MAX - info.ri_system_time) return false;
    /* libproc CPU counters are Mach ticks, including on Apple Silicon. */
    __uint128_t ns=(__uint128_t)(info.ri_user_time+info.ri_system_time) *
        s->cpu_timebase_numer / s->cpu_timebase_denom;
    if (ns>UINT64_MAX) return false;
    *cpu=(uint64_t)ns; return true;
#else
    (void)s; (void)pid; (void)cpu; return false;
#endif
}
bool mg_supervisor_sample_cpu(void *context, uint32_t domain, uint64_t *cpu) {
    mg_supervisor *s = context;
    if (!s || !cpu || domain >= MG_GOV_DOMAINS) return false;
    mg_supervised_child *c = &s->children[domain];
    return c->used && !c->exited && process_cpu(s, c->pid, cpu);
}
static uint64_t time_ns(struct timeval t) {
    if (t.tv_sec < 0 || (uint64_t)t.tv_sec > (UINT64_MAX - (uint64_t)t.tv_usec * 1000) / UINT64_C(1000000000))
        return UINT64_MAX;
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_usec * 1000;
}
mg_work_result mg_supervisor_poll(mg_supervisor *s, uint64_t now) {
    if (!s || !s->governance || now < s->now_ns) return MG_WORK_INVALID;
    s->now_ns = now; mg_work_result result = MG_WORK_OK;
    for (uint32_t i=0; i<MG_GOV_DOMAINS; ++i) {
        mg_supervised_child *c = &s->children[i]; if (!c->used || c->exited) continue;
        if (c->identity_held && now >= c->identity_deadline_ns) {
            c->identity_held=false;
            (void)mg_supervisor_stop(s,i,c->launch_id,now);
        }
        if (c->identity_held) {
            siginfo_t info; memset(&info,0,sizeof(info));
            if (waitid(P_PID,(id_t)c->pid,&info,WEXITED|WNOHANG|WNOWAIT)<0) {
                result=MG_WORK_STALE; continue;
            }
            c->exit_pending=info.si_pid==c->pid;
            if (c->exit_pending) continue;
        }
        uint64_t cpu;
        bool measured = process_cpu(s, c->pid, &cpu);
        c->cpu_available=measured;
        if (measured) {
            c->last_cpu_ns=cpu;
            for (uint32_t j=0; j<MG_GOV_COMPLETIONS; ++j) {
                mg_gov_running *run = &s->governance->running[j];
                if (run->used && run->domain == i) {
                    mg_work_result r = mg_gov_sample(s->governance, j, now, cpu, automatic_stop, s);
                    if (r != MG_WORK_OK) result = r;
                }
            }
        }
        struct rusage usage; int status; memset(&usage, 0, sizeof(usage));
        pid_t reaped = wait4(c->pid, &status, WNOHANG, &usage);
        if (reaped == c->pid) {
            c->exited=true; c->status=status;
            c->exit_pending=false;
            if (c->gate_fd >= 0) { close(c->gate_fd); c->gate_fd=-1; }
            uint64_t u=time_ns(usage.ru_utime), v=time_ns(usage.ru_stime);
            cpu = u > UINT64_MAX-v ? UINT64_MAX : u+v;
            c->last_cpu_ns=cpu; c->cpu_available=true;
            for (uint32_t j=0; j<MG_GOV_COMPLETIONS; ++j) {
                mg_gov_running *run=&s->governance->running[j];
                if (run->used && run->domain == i && cpu >= run->cpu_start_ns)
                    (void)mg_gov_finish(s->governance, j, now, cpu - run->cpu_start_ns);
            }
            continue;
        }
        if (reaped < 0) { result=MG_WORK_STALE; continue; }
        if (!measured) result = MG_WORK_BUDGET;
        if (!measured && !c->stopping) {
            /* Missing accounting cannot become a free-running grant. */
            (void)mg_supervisor_stop(s, i, c->launch_id, now);
        }
        /* An unreaped direct child retains its PID; caller must not reap it. */
        if (c->stopping && !c->terminated && now - c->stop_ns >= UINT64_C(500000000)) {
            if (kill(c->pid, SIGTERM) == 0 || errno == ESRCH) c->terminated=true;
        }
        if (c->stopping && !c->killed && now - c->stop_ns >= UINT64_C(1500000000)) {
            if (kill(c->pid, SIGKILL) == 0 || errno == ESRCH) c->killed=true;
        }
    }
    return result;
}
