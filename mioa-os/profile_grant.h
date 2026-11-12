/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_PROFILE_GRANT_H
#define MIOA_GRAPHICS_PROFILE_GRANT_H
#include "memory.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Trusted supervisor binds one socket to one enrolled principal. The memory
 * account is the resource owner's sole ledger; all operations run on its
 * serialized executor. EOF or protocol failure never refunds live tickets. */
#define MG_PROFILE_GRANT_TICKETS 256

typedef struct {
    mg_memory *memory;
    mg_lease_key account, resource;
    mg_memory_ticket tickets[MG_PROFILE_GRANT_TICKETS];
    bool live[MG_PROFILE_GRANT_TICKETS];
    uint64_t last_request;
    int fd;
    unsigned char request[48], reply[32];
    size_t input, output;
    bool reply_pending, closed;
} mg_profile_grant;

mg_memory_result mg_profile_grant_init(mg_profile_grant *, mg_memory *, int fd,
                                       mg_lease_key account, mg_lease_key resource);
/* Nonblocking: performs at most one read or write; BUSY is retryable. A closed
 * or uncertain channel keeps all outstanding charges in the parent ledger. */
mg_memory_result mg_profile_grant_pump(mg_profile_grant *);
#endif
