#ifndef MIOA_THREAD_V1_H
#define MIOA_THREAD_V1_H

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_thread_status_v1 {
    MIOA_THREAD_OK = 0,
    MIOA_THREAD_INVALID = 1,
    MIOA_THREAD_NO_MEMORY = 2,
    MIOA_THREAD_START_FAILED = 3
} mioa_thread_status_v1;

typedef void (*mioa_thread_entry_v1)(void *context);

/* Starts one Host-owned thread. On success, *thread_handle is an opaque,
 * caller-owned join token. context is borrowed until join returns. */
mioa_thread_status_v1 mioa_thread_start_v1(
    mioa_thread_entry_v1 entry, void *context, void **thread_handle);
/* Starts and detaches before returning; no join token is produced. The caller
 * keeps context valid until entry returns. */
mioa_thread_status_v1 mioa_thread_start_detached_v1(
    mioa_thread_entry_v1 entry, void *context);

/* Blocks until completion, frees the provider-owned token, and clears it.
 * Null token pointers and empty tokens are no-ops. Join is single-consumer. */
void mioa_thread_join_v1(void **thread_handle);

/* Relinquishes the token without waiting, frees the provider-owned token,
 * and clears it. The callback context must remain valid until entry returns.
 * Null token pointers and empty tokens are no-ops. */
void mioa_thread_detach_v1(void **thread_handle);

/* Yield execution to another ready thread. */
void mioa_thread_yield_v1(void);

#ifdef __cplusplus
}
#endif
#endif
