/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_SUPERVISOR_H
#define MIOA_GRAPHICS_SUPERVISOR_H
#include "governance.h"
#include <sys/types.h>

/* Provisional trusted direct-child qualification endpoint, NOT a containment
 * boundary or independent sidecar. Never attach arbitrary PIDs; caller owns all
 * wait operations. Children can escape or create descendants without sandbox. */
typedef struct {
    pid_t pid;
    int gate_fd;
    uint64_t launch_id, stop_ns, last_cpu_ns, identity_deadline_ns;
    int status;
    /* cpu_available describes the latest attempted sample or final wait4 usage. */
    bool used, released, stopping, terminated, killed, exited, cpu_available;
    bool identity_held, exit_pending, automatic_stop;
} mg_supervised_child;
typedef struct {
    mg_governance *governance;
    mg_supervised_child children[MG_GOV_DOMAINS];
    uint64_t next_launch_id, now_ns;
    uint32_t cpu_timebase_numer, cpu_timebase_denom;
} mg_supervisor;
void mg_supervisor_init(mg_supervisor *, mg_governance *);
/* Resolve the OS descriptor ceiling before fork; false means fail closed. */
bool mg_supervisor_fd_ceiling(int *ceiling);
/* Called only in a freshly forked, single-threaded child. */
bool mg_supervisor_close_extra(int first);
/* argv[0] and absolute executable are caller-supplied trusted executable.
 * Child blocks before exec until release; only gate + stdio survive fork. */
mg_work_result mg_supervisor_spawn(mg_supervisor *, uint32_t domain,
                                    const char *executable, char *const argv[],
                                    uint64_t *launch_id, pid_t *pid);
/* Optional private endpoint: child receives FD 4, caller owns *endpoint_fd.
 * envp is the complete environment; NULL retains the caller environment. */
mg_work_result mg_supervisor_spawn_endpoint(mg_supervisor *, uint32_t domain,
    const char *executable, char *const argv[], char *const envp[],
    uint64_t *launch_id, pid_t *pid, int *endpoint_fd);
/* Optional second event lane is delivered to the child as FD 5. */
mg_work_result mg_supervisor_spawn_channels(mg_supervisor *, uint32_t domain,
    const char *executable, char *const argv[], char *const envp[],
    uint64_t *launch_id, pid_t *pid, int *endpoint_fd, int *event_fd);
mg_work_result mg_supervisor_spawn_profile(mg_supervisor *, uint32_t domain,
    const char *executable, char *const argv[], char *const envp[],
    uint64_t *launch_id, pid_t *pid, int *endpoint_fd, int *event_fd, int *idb_fd, int *grant_fd, int *task_fd);
mg_work_result mg_supervisor_release(mg_supervisor *, uint32_t domain, uint64_t launch_id);
/* Pass mg_supervisor_sample_cpu and the supervisor as mg_gov_next's callback. */
bool mg_supervisor_sample_cpu(void *supervisor, uint32_t domain, uint64_t *cpu_ns);
/* Successful Stop advances the owner clock, including idempotent repeats.
 * Repeats preserve the first stop deadline; older Stop/poll times are invalid. */
mg_work_result mg_supervisor_stop(mg_supervisor *, uint32_t domain, uint64_t launch_id, uint64_t now_ns);
/* Only application slots (0 or >=4) may be reused, after this supervisor has
 * reaped the exact stopped launch and settled all its CPU fragments. */
mg_work_result mg_supervisor_rearm(mg_supervisor *, uint32_t domain,
                                    uint64_t launch_id, mg_work_domain_ref *next);
/* Called by independent owner at 100ms. Samples actual OS process CPU, invokes
 * automatic overrun stop, escalates SIGTERM at 500ms and SIGKILL at 1500ms,
 * and settles CPU only on wait4 exit. Returns BUDGET when a live child's CPU
 * is unavailable. On macOS, wait4 usage may include reaped descendant CPU;
 * it is not necessarily scoped like the live direct-process sample. */
mg_work_result mg_supervisor_poll(mg_supervisor *, uint64_t now_ns);
#endif
