#ifndef MIOA_OS_FILES_H
#define MIOA_OS_FILES_H

#include <stdbool.h>
#include <stddef.h>
#include "api/system/graphics_file_v1.h"

/* Private trusted Host interface. Core must only include graphics_file_v1.h.
 * One owner/endpoint per authenticated connection; nonzero epoch is unique
 * across owner lifetimes. Host serializes grants, calls, revoke and destroy.
 * This local provider refuses MG_FILE_ISOLATED before allocation. */
typedef struct mg_os_files mg_os_files;
mg_file_status_v1 mg_os_files_create(uint32_t capacity, uint64_t epoch,
    uint64_t required, mg_os_files **owner, mg_file_endpoint_v1 **endpoint);

/* Duplicate a Host-authorized regular-file descriptor. Source remains owned
 * by Host. No path lookup, creation, truncation or implicit expansion of rights.
 * base + length <= INT64_MAX; rights subset of the descriptor's access mode.
 * O_APPEND is refused at grant. The duplicate shares file status flags:
 * Host must not enable O_APPEND on any alias while a grant exists. Linux
 * pwrite can otherwise append outside the granted range; this is not enforced. */
mg_file_status_v1 mg_os_files_grant(mg_os_files *owner, int source,
    uint32_t rights, uint64_t base, uint64_t length, mg_file_handle_v1 *handle);
mg_file_status_v1 mg_os_files_revoke(mg_os_files *owner, mg_file_handle_v1 handle);
/* Consumes owner, closes all held descriptors, reports IO if any close failed.
 * No endpoint or borrowed owner reference may be used afterwards. */
mg_file_status_v1 mg_os_files_destroy(mg_os_files *owner);

/* Direct frame ring slot write for LPFR v2 raw frame export. */
bool mg_os_raw_frame_write(const char *path, size_t path_len, uint32_t slot,
                          const uint8_t *bytes, size_t len, uint64_t capacity);

#endif
