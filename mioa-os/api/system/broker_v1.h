/* SPDX-License-Identifier: MIT */
#ifndef MIOA_BROKER_V1_H
#define MIOA_BROKER_V1_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint64_t row_stride;
    uint64_t byte_length;
    uint64_t allocation_bytes;
} mioa_backing_metadata_v1;

typedef bool (*mioa_paint_fn_v1)(void *context, uint8_t *pixels, size_t row_stride, size_t byte_length);

uint32_t lp_broker_paint(
    const char *name,
    uint64_t epoch,
    int32_t host,
    uint32_t uid,
    int32_t version,
    uint32_t timeout,
    uint32_t width,
    uint32_t height,
    void *context,
    mioa_paint_fn_v1 paint,
    mioa_backing_metadata_v1 *metadata
);

bool lp_trusted_cdp_peer(int fd, int32_t host, uint32_t uid, int32_t version);

#ifdef __cplusplus
}
#endif

#endif /* MIOA_BROKER_V1_H */
