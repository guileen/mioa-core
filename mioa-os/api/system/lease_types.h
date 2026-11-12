/* SPDX-License-Identifier: MIT */
#ifndef MIOA_OS_LEASE_TYPES_H
#define MIOA_OS_LEASE_TYPES_H
#include <stdint.h>

/* Public source-level identity, not a capability, wire layout, or stable ABI.
 * An authenticated authority assigns identities. scope binds the relevant
 * Host/connection lifetime, resource identifies an object, and generation
 * prevents reuse. All fields are nonzero; never reuse a complete identity. */
typedef struct {
    uint64_t scope, resource, generation;
} mg_lease_key;
#endif
