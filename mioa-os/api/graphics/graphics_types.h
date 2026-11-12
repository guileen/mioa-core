/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_TYPES_H
#define MIOA_GRAPHICS_TYPES_H
#include "../system/lease_types.h"
#include <stdint.h>

/* Negotiated linear, single-level 2D image interchange only. Other bindings
 * describe tiled/compressed/depth/3D resources; this is not a Web API limit. */
typedef enum {
    MG_GPU_R8, MG_GPU_RG8, MG_GPU_RGBA8, MG_GPU_BGRA8, MG_GPU_NV12,
    MG_GPU_FORMAT_COUNT
} mg_gpu_format;
/* Only call with an enumerator in [0, MG_GPU_FORMAT_COUNT). */
#define MG_GPU_FORMAT_BIT(f) (UINT32_C(1) << (f))
enum { MG_GPU_READ = 1, MG_GPU_WRITE = 2 };

typedef struct { uint64_t offset, row_stride; } mg_gpu_plane;
typedef struct {
    mg_lease_key resource, device, owner;
    uint64_t allocation_bytes;
    uint32_t width, height, rights, plane_count;
    mg_gpu_format format;
    mg_gpu_plane planes[2];
} mg_gpu_image;

typedef struct {
    uint64_t max_allocation_bytes;
    uint32_t max_width, max_height, row_alignment, offset_alignment;
    uint32_t formats;
} mg_gpu_limits;

typedef struct { uint64_t offset, end, row_bytes; uint32_t rows; } mg_gpu_span;
typedef struct { uint32_t plane_count; mg_gpu_span planes[2]; } mg_gpu_layout;
#endif
