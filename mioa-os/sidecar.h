/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_SIDECAR_H
#define MIOA_GRAPHICS_SIDECAR_H
#include "supervisor.h"

/* Private same-machine qualification protocol, not a public or network ABI.
 * The inherited socket and random nonce bind requests to this host launch.
 * Only trusted executables may be launched: there is no sandbox here. */
#define MG_SIDECAR_ARGS 16
#define MG_SIDECAR_ARG_BYTES 256
typedef enum {
    MG_SIDECAR_REGISTER = 1, MG_SIDECAR_SPAWN, MG_SIDECAR_RELEASE,
    MG_SIDECAR_SUBMIT, MG_SIDECAR_DISPATCH, MG_SIDECAR_STOP,
    MG_SIDECAR_FINISH, MG_SIDECAR_QUERY, MG_SIDECAR_IDENTITY_ACK,
    MG_SIDECAR_ESCROW, MG_SIDECAR_ESCROW_RELEASE, MG_SIDECAR_POLICY,
    MG_SIDECAR_REARM, MG_SIDECAR_CHANNELS_ACK,
    MG_SIDECAR_IO_BEGIN, MG_SIDECAR_IO_FINISH
} mg_sidecar_operation;
typedef enum { MG_SIDECAR_RUNTIME=0, MG_SIDECAR_COOKIE_OWNER=1,
               MG_SIDECAR_IDB_OWNER=2 } mg_sidecar_role;
/* POLICY updates class and monotonically advances policy/cap epochs; quota_ns
 * is zero or the unchanged registered cap. It does not refill CPU tokens and
 * does not attest that a native execution gate has applied the new policy. */
/* FINISH is a trusted host assertion that the admitted CPU fragment has
 * actually stopped; the sidecar measures its CPU but cannot prove a generic
 * executable yielded. Do not use without a qualified execution gate. */
/* SPAWN requires the registered key. REARM requires the exact stopped launch
 * and its old key, and returns a fresh key only after owned wait4 settlement.
 * Application slots are 0 and >=4; service slots 1/2/3 cannot be reincarnated.
 * POLICY/SUBMIT/FINISH require the live launch; FINISH also requires task.id.
 * Rearm preserves policy epochs, quota, CPU debt and all unrelated resources. */
typedef struct {
    uint8_t nonce[16];
    uint64_t request_id, launch_id, quota_ns, policy_epoch, cap_epoch, cpu_ns;
    mg_lease_key key;
    mg_gov_request task;
    uint32_t operation, domain, slot, class_id, argc;
    /* Explicit trusted-only profile; 0 or any other value is rejected. */
    uint32_t trusted_executable, with_endpoint, with_events, with_idb, with_grant, with_task, role, hold_identity, trusted_cdp;
    uint64_t io_ticket, io_bytes;
    uint32_t io_kind;
    /* Optional absolute path to a host-owned post-exec raster listener. */
    char raster_socket[MG_SIDECAR_ARG_BYTES];
    char owner_path[MG_SIDECAR_ARG_BYTES];
    char argv[MG_SIDECAR_ARGS][MG_SIDECAR_ARG_BYTES];
} mg_sidecar_request;
typedef struct {
    uint64_t request_id, launch_id, cpu_ns;
    mg_lease_key key;
    int64_t pid;
    uint32_t result, domain, slot;
    bool exited, stopping, cpu_available, has_endpoint, has_events, has_idb, has_grant, has_task, exit_pending, identity_held, automatic_stop;
    int status;
    mg_gov_request task;
    uint64_t io_ticket, io_network_bytes, io_disk_bytes;
    uint32_t io_pending;
    bool io_counter_overflow;
} mg_sidecar_reply;
typedef struct {
    int fd, received_fd, received_event_fd, received_idb_fd, received_grant_fd, received_task_fd, tx_fd;
    int received_raw[5];
    uint32_t received_count;
    pid_t pid;
    uint8_t nonce[16];
    mg_sidecar_request tx;
    mg_sidecar_reply rx;
    size_t tx_offset, rx_offset;
    bool tx_pending, awaiting_reply, acknowledging_channels;
} mg_sidecar_client;
/* Starts a separate direct-child owner. Closing fd is the host-liveness event;
 * host must eventually waitpid(pid). One outstanding request at a time. */
mg_work_result mg_sidecar_start(mg_sidecar_client *, uint32_t concurrency);
mg_work_result mg_sidecar_send(mg_sidecar_client *, const mg_sidecar_request *);
/* Duplicates one lease FD and sends it in an ESCROW request, indexed by domain.
 * After success the caller may close its original; the client retains its copy
 * until the complete reply proves receipt. Close copies without LOCK_UN.
 * Release is refused while any sidecar child remains unreaped. */
mg_work_result mg_sidecar_send_lease(mg_sidecar_client *, const mg_sidecar_request *, int lease_fd);
/* Progress partial nonblocking send/receive; BUSY means retry in a later turn. */
/* SPAWN with_endpoint returns received_fd (child FD4). with_events additionally
 * returns received_event_fd (child FD5). Caller takes each and resets it to -1
 * before the next request. The event lane requires the Profile owner to enroll
 * and write it; the sidecar does not implement event semantics.
 * Receipt ACKs are internal to mg_sidecar_receive: FD-bearing replies remain
 * BUSY until the exact nonce/request/domain/launch ACK has been sent. The
 * supervisor retains its copies until that ACK, including across partial I/O. */
/* Authenticate a connected Unix socket's kernel peer PID/euid. For an opted-in
 * raster launch, success is provisional until IDENTITY_ACK for the same domain
 * and launch_id succeeds over this live sidecar connection; the sidecar holds
 * the child unreaped until that ACK. Close the peer on any failure. */
mg_work_result mg_sidecar_verify_peer(int connected_fd, pid_t expected_pid, uid_t expected_uid);
mg_work_result mg_sidecar_verify_peer_version(int connected_fd, pid_t expected_pid,
                                              uid_t expected_uid, int expected_pidversion);
/* Returns bytes sent, or -1 with errno. Nonblocking; never closes supplied FDs.
 * Retain sender copies until an application-level receiver receipt ACK; a
 * positive sendmsg alone is not a safe socket lifetime boundary on Darwin.
 * Supply FDs only until the first positive return; thereafter retry remaining
 * bytes with count=0, otherwise SCM_RIGHTS would be delivered twice. Max 8. */
ssize_t mg_sidecar_send_fds(int socket_fd, const void *bytes, size_t length,
                            const int *fds, size_t count);
/* *count is input capacity (0..8) and output actual descriptors. On success
 * caller owns all returned CLOEXEC FDs; malformed/truncated ancillary data
 * closes visible received FDs, returns -1 and requires discarding the stream.
 * The local receiver reserves space for XNU's 512-right ceiling before applying
 * the protocol limit. Other platform ceilings require separate qualification. */
ssize_t mg_sidecar_recv_fds(int socket_fd, void *bytes, size_t length,
                            int *fds, size_t *count);
mg_work_result mg_sidecar_receive(mg_sidecar_client *, mg_sidecar_reply *);
#endif
