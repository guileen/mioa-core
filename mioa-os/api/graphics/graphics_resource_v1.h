/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_RESOURCE_V1_H
#define MIOA_GRAPHICS_RESOURCE_V1_H
#include <stdint.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
#define MG_RESOURCE_V1_VERSION UINT32_C(0x00010000)
#define MG_RESOURCE_V1_MAX_REQUEST_BYTES UINT32_C(4096)
#define MG_RESOURCE_OK 0
#define MG_RESOURCE_PENDING 1
#define MG_RESOURCE_INVALID (-1)
#define MG_RESOURCE_UNAUTHORIZED (-2)
#define MG_RESOURCE_STALE (-3)
#define MG_RESOURCE_CLOSED (-4)
#define MG_RESOURCE_LIMIT (-5)
#define MG_RESOURCE_FULL (-6)
#define MG_RESOURCE_NOT_READY (-7)
#define MG_RESOURCE_UNSUPPORTED (-8)
#define MG_RESOURCE_BACKEND_FAILURE (-9)
#define MG_RESOURCE_EXHAUSTED (-10)
#define MG_RESOURCE_READ UINT32_C(1)
#define MG_RESOURCE_WRITE UINT32_C(2)
#define MG_RESOURCE_CPU_BUFFER UINT32_C(1)
#define MG_RESOURCE_LINEAR_GPU_IMAGE UINT32_C(2)
#define MG_RESOURCE_FORMAT_NONE UINT32_C(0)
#define MG_RESOURCE_FORMAT_R8 UINT32_C(1)
#define MG_RESOURCE_FORMAT_RG8 UINT32_C(2)
#define MG_RESOURCE_FORMAT_RGBA8 UINT32_C(3)
#define MG_RESOURCE_FORMAT_BGRA8 UINT32_C(4)
#define MG_RESOURCE_FORMAT_NV12 UINT32_C(5)
#define MG_RESOURCE_FEATURE_CPU_BUFFER UINT64_C(1)
#define MG_RESOURCE_FEATURE_LOCAL_CPU_BORROW UINT64_C(2)
#define MG_RESOURCE_FEATURE_LINEAR_GPU_IMAGE UINT64_C(4)
#define MG_RESOURCE_FEATURE_METAL_SHARED_IMAGE UINT64_C(8)
#define MG_RESOURCE_WRITABLE UINT32_C(1)
#define MG_RESOURCE_PUBLISHED UINT32_C(2)
#define MG_RESOURCE_RETIRING UINT32_C(3)
#define MG_RESOURCE_DISPOSAL_PENDING UINT32_C(4)
#define MG_RESOURCE_RELEASED UINT32_C(5)
#define MG_RESOURCE_NEGOTIATE UINT32_C(0)
#define MG_RESOURCE_CREATE UINT32_C(1)
#define MG_RESOURCE_WRITE_BEGIN UINT32_C(2)
#define MG_RESOURCE_WRITE_END UINT32_C(3)
#define MG_RESOURCE_PUBLISH UINT32_C(4)
#define MG_RESOURCE_READ_BEGIN UINT32_C(5)
#define MG_RESOURCE_READ_END UINT32_C(6)
#define MG_RESOURCE_DESCRIBE UINT32_C(7)
#define MG_RESOURCE_CLOSE UINT32_C(8)
#define MG_RESOURCE_STATUS UINT32_C(9)
typedef struct mg_resource_endpoint_v1 mg_resource_endpoint_v1;
typedef struct { uint64_t scope, object, generation; } mg_resource_handle_v1;
typedef struct { uint32_t struct_size, version; uint64_t required_features, optional_features; } mg_resource_negotiate_request_v1;
typedef struct { uint32_t struct_size, version; uint64_t selected_features; mg_resource_handle_v1 device; } mg_resource_negotiate_response_v1;
typedef struct { uint32_t struct_size, version, kind, access; uint64_t size_bytes; uint32_t width, height, format, flags; mg_resource_handle_v1 device; } mg_resource_create_request_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 resource; } mg_resource_create_response_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 resource; uint64_t offset, length; } mg_resource_begin_request_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 use; uint64_t length; void *data; } mg_resource_begin_response_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 use; } mg_resource_end_request_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 resource; } mg_resource_request_v1;
typedef struct { uint32_t struct_size, version, state, reserved; } mg_resource_state_response_v1;
typedef struct { uint64_t offset, row_stride; } mg_resource_plane_v1;
typedef struct { uint32_t struct_size, version; mg_resource_handle_v1 resource, device; uint32_t kind, state, access, format, width, height, plane_count, reserved; uint64_t allocation_bytes, logical_bytes; mg_resource_plane_v1 plane[2]; uint64_t reserved_tail; } mg_resource_describe_response_v1;
int32_t mg_resource_call_v1(mg_resource_endpoint_v1 *endpoint, uint64_t endpoint_epoch, uint32_t opcode, const void *request, uint32_t request_bytes, void *response, uint32_t response_bytes);
#ifdef __cplusplus
}
#define MG_RESOURCE_ASSERT static_assert
#else
#define MG_RESOURCE_ASSERT _Static_assert
#endif
MG_RESOURCE_ASSERT(sizeof(void *) == 8 && sizeof(uint64_t) == 8, "resource v1 requires 64-bit pointers");
MG_RESOURCE_ASSERT(sizeof(mg_resource_handle_v1) == 24, "handle layout");
MG_RESOURCE_ASSERT(sizeof(mg_resource_negotiate_request_v1) == 24 && sizeof(mg_resource_negotiate_response_v1) == 40, "negotiate layout");
MG_RESOURCE_ASSERT(sizeof(mg_resource_create_request_v1) == 64 && offsetof(mg_resource_create_request_v1, device) == 40 && sizeof(mg_resource_create_response_v1) == 32, "create layout");
MG_RESOURCE_ASSERT(sizeof(mg_resource_begin_request_v1) == 48 && sizeof(mg_resource_begin_response_v1) == 48 && offsetof(mg_resource_begin_response_v1, data) == 40, "begin layout");
MG_RESOURCE_ASSERT(sizeof(mg_resource_end_request_v1) == 32 && sizeof(mg_resource_request_v1) == 32 && sizeof(mg_resource_state_response_v1) == 16, "operation layout");
MG_RESOURCE_ASSERT(sizeof(mg_resource_describe_response_v1) == 144 && offsetof(mg_resource_describe_response_v1, allocation_bytes) == 88 && offsetof(mg_resource_describe_response_v1, plane) == 104 && offsetof(mg_resource_describe_response_v1, reserved_tail) == 136, "describe layout");
#define MG_RESOURCE_OFFSET(type, field, value) MG_RESOURCE_ASSERT(offsetof(type, field) == (value), "resource v1 offset: " #type "." #field)
MG_RESOURCE_OFFSET(mg_resource_handle_v1, scope, 0);
MG_RESOURCE_OFFSET(mg_resource_handle_v1, object, 8);
MG_RESOURCE_OFFSET(mg_resource_handle_v1, generation, 16);
MG_RESOURCE_OFFSET(mg_resource_negotiate_request_v1, struct_size, 0);
MG_RESOURCE_OFFSET(mg_resource_negotiate_request_v1, version, 4);
MG_RESOURCE_OFFSET(mg_resource_negotiate_request_v1, required_features, 8);
MG_RESOURCE_OFFSET(mg_resource_negotiate_request_v1, optional_features, 16);
MG_RESOURCE_OFFSET(mg_resource_negotiate_response_v1, selected_features, 8);
MG_RESOURCE_OFFSET(mg_resource_negotiate_response_v1, device, 16);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, kind, 8);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, access, 12);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, size_bytes, 16);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, width, 24);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, height, 28);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, format, 32);
MG_RESOURCE_OFFSET(mg_resource_create_request_v1, flags, 36);
MG_RESOURCE_OFFSET(mg_resource_create_response_v1, resource, 8);
MG_RESOURCE_OFFSET(mg_resource_begin_request_v1, resource, 8);
MG_RESOURCE_OFFSET(mg_resource_begin_request_v1, offset, 32);
MG_RESOURCE_OFFSET(mg_resource_begin_request_v1, length, 40);
MG_RESOURCE_OFFSET(mg_resource_begin_response_v1, use, 8);
MG_RESOURCE_OFFSET(mg_resource_begin_response_v1, length, 32);
MG_RESOURCE_OFFSET(mg_resource_end_request_v1, use, 8);
MG_RESOURCE_OFFSET(mg_resource_request_v1, resource, 8);
MG_RESOURCE_OFFSET(mg_resource_state_response_v1, state, 8);
MG_RESOURCE_OFFSET(mg_resource_state_response_v1, reserved, 12);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, resource, 8);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, device, 32);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, kind, 56);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, state, 60);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, access, 64);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, format, 68);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, width, 72);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, height, 76);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, plane_count, 80);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, reserved, 84);
MG_RESOURCE_OFFSET(mg_resource_describe_response_v1, logical_bytes, 96);
MG_RESOURCE_OFFSET(mg_resource_plane_v1, offset, 0);
MG_RESOURCE_OFFSET(mg_resource_plane_v1, row_stride, 8);
#undef MG_RESOURCE_OFFSET
#undef MG_RESOURCE_ASSERT
#endif
