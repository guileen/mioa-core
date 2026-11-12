#ifndef MIOA_GRAPHICS_FILE_V1_H
#define MIOA_GRAPHICS_FILE_V1_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MG_FILE_V1_VERSION UINT32_C(1)
#define MG_FILE_V1_MAX_TRANSFER UINT64_C(1048576)
#define MG_FILE_READ UINT32_C(1)
#define MG_FILE_WRITE UINT32_C(2)
#define MG_FILE_POSITIONAL UINT64_C(1)
#define MG_FILE_REVOCATION UINT64_C(2)
#define MG_FILE_ISOLATED UINT64_C(4)

typedef struct mg_file_endpoint_v1 mg_file_endpoint_v1;
typedef struct mg_file_handle_v1 {
    uint64_t connection;
    uint64_t object;
} mg_file_handle_v1;

typedef enum mg_file_status_v1 {
    MG_FILE_OK = 0,
    MG_FILE_INVALID = 1,
    MG_FILE_UNSUPPORTED = 2,
    MG_FILE_DENIED = 3,
    MG_FILE_STALE = 4,
    MG_FILE_LIMIT = 5,
    MG_FILE_IO = 6
} mg_file_status_v1;

/* Trusted native binding, not a wire format. Host serializes all calls.
 * Endpoint lifetime belongs to Host. Client cannot create/grant an endpoint.
 * Failed negotiation leaves selected unchanged and grants no new features. */
mg_file_status_v1 mg_file_negotiate_v1(mg_file_endpoint_v1 *endpoint,
    uint32_t version, uint64_t required, uint64_t *selected);

/* Offset is relative to the Host-granted byte range, never a host path/FD.
 * At most MAX_TRANSFER bytes per call; short I/O and zero-byte EOF are success.
 * No hidden seek, retry of short I/O, allocation, sync or implicit close.
 * count is written only on OK. On IO, writes are not safe to replay blindly.
 * Borrowed buffers live through the synchronous call and must not overlap count. */
mg_file_status_v1 mg_file_read_v1(mg_file_endpoint_v1 *endpoint,
    mg_file_handle_v1 handle, uint64_t offset, void *buffer, uint64_t length,
    uint64_t *count);
mg_file_status_v1 mg_file_write_v1(mg_file_endpoint_v1 *endpoint,
    mg_file_handle_v1 handle, uint64_t offset, const void *buffer,
    uint64_t length, uint64_t *count);

/* Close is allowed after revocation. It consumes the handle even on IO;
 * duplicate close is STALE, never retry a native close with the old identity. */
mg_file_status_v1 mg_file_close_v1(mg_file_endpoint_v1 *endpoint,
    mg_file_handle_v1 handle);

#ifdef __cplusplus
}
#endif
#endif
