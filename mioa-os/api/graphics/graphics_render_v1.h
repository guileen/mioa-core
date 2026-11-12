/* SPDX-License-Identifier: MIT */
#ifndef MIOA_GRAPHICS_RENDER_V1_H
#define MIOA_GRAPHICS_RENDER_V1_H
#include <stdint.h>
#include <stddef.h>
#include "graphics_resource_v1.h"
#ifdef __cplusplus
extern "C" {
#endif
#define MG_RENDER_V1_VERSION UINT32_C(0x00010000)
#define MG_RENDER_FEATURE_SHARED_TARGET UINT64_C(1)
#define MG_RENDER_FEATURE_SCENE UINT64_C(2)
#define MG_RENDER_FEATURE_PRESENTED_HIT UINT64_C(4)
#define MG_RENDER_FEATURE_STATS UINT64_C(8)
#define MG_RENDER_TRANSFER_NAME_MAX 128
#define MG_RENDER_TRANSFER_AUTHENTICATED_LOCAL_IMAGE UINT32_C(1)
#define MG_RENDER_TRANSFER_VERSION UINT32_C(1)
/* Local authenticated service endpoint; never a platform/native object. */
typedef struct mg_render_endpoint_v1 mg_render_endpoint_v1;
typedef struct { uint64_t scope, id, generation; } mg_render_key_v1;
typedef struct { uint32_t struct_size, version; } mg_render_header_v1;
typedef struct { double x, y, width, height; } mg_render_rect_v1;
typedef struct { uint32_t struct_size, version; uint64_t required_features, optional_features; } mg_render_negotiate_v1;
typedef struct { uint32_t struct_size, version; uint64_t selected_features, host_epoch, device_epoch; uint32_t max_surfaces, max_views; } mg_render_capabilities_v1;
typedef struct { uint32_t struct_size, version, width, height, format, reserved; uint64_t configuration; } mg_render_surface_config_v1;
typedef struct { uint32_t struct_size, version; mg_render_key_v1 surface; uint64_t configuration; } mg_render_acquire_v1;
typedef struct {
    uint32_t struct_size, version; mg_render_key_v1 surface, lease;
    uint64_t configuration, transfer_epoch, byte_length, row_stride;
    uint32_t width, height, format, transfer_name_length, transfer_kind, transfer_version;
    char transfer_name[MG_RENDER_TRANSFER_NAME_MAX];
} mg_render_target_v1;
/* Portable trusted-local binding. connected_socket pins the host's process
 * incarnation; process/uid are expected peer credentials, not authorization. */
typedef struct { uint32_t struct_size, version; int64_t host_process; uint32_t host_uid, timeout_ms; int32_t connected_socket; uint32_t reserved; } mg_render_transfer_connection_v1;
/* Caller-owned opaque storage: zero initialize, never copy while mapped. */
typedef struct { uint64_t opaque[4]; } mg_render_writer_v1;
typedef struct { uint32_t struct_size, version; void *bytes; uint64_t byte_length, row_stride; } mg_render_write_mapping_v1;
int32_t mg_render_transfer_open_v1(const mg_render_target_v1 *, const mg_render_transfer_connection_v1 *, mg_render_writer_v1 *, mg_render_write_mapping_v1 *);
/* Releases every native alias before successful return. The caller must then
 * send its exact authenticated operation completion to the service. */
int32_t mg_render_transfer_close_v1(mg_render_writer_v1 *);
/* Evidence is an authenticated operation identity verified by the service's
 * registered trusted executor adapter, not a provider assertion of completion. */
typedef struct { uint32_t struct_size, version; mg_render_key_v1 lease; uint64_t operation; } mg_render_finish_v1;
typedef struct { uint32_t struct_size, version; mg_render_key_v1 surface, content; uint64_t configuration, sequence; } mg_render_publication_v1;
typedef struct {
    mg_render_key_v1 view, surface, content;
    uint64_t binding_revision, configuration, adapter_binding;
    mg_render_rect_v1 destination, clip;
    double transform[6]; float opacity; int32_t z; uint32_t interactive, reserved;
} mg_render_view_v1;
typedef struct {
    uint32_t struct_size, version; uint64_t request_id, expected_revision, authority_epoch;
    uint32_t count, reserved; const mg_render_view_v1 *views;
} mg_render_scene_v1;
typedef struct { uint32_t struct_size, version; uint64_t request_id, revision, submission, authority_epoch; uint32_t phase; int32_t status; double presented_time; } mg_render_event_v1;
#define MG_RENDER_EVENT_ACCEPTED UINT32_C(1)
#define MG_RENDER_EVENT_SUBMITTED UINT32_C(2)
/* COMPLETED is native-terminal, not callback-terminal; status distinguishes
 * native success/failure. Keep frozen geometry until PRESENTED, RETIRED or
 * FAILED. FAILED is published only after callback-safe retirement. */
#define MG_RENDER_EVENT_COMPLETED UINT32_C(3)
#define MG_RENDER_EVENT_PRESENTED UINT32_C(4)
#define MG_RENDER_EVENT_FAILED UINT32_C(5)
#define MG_RENDER_EVENT_UNKNOWN UINT32_C(6)
#define MG_RENDER_EVENT_REVOKED UINT32_C(7)
#define MG_RENDER_EVENT_DEVICE_LOST UINT32_C(8)
#define MG_RENDER_EVENT_RETIRED UINT32_C(9)
typedef struct { uint32_t struct_size, version; double x, y; uint64_t focus_epoch; } mg_render_hit_query_v1;
typedef struct { uint32_t struct_size, version; mg_render_key_v1 view, surface, content; uint64_t scene_revision, binding_revision, configuration, adapter_binding, focus_epoch; double x, y; } mg_render_hit_v1;
#define MG_RENDER_STATS_BACKING UINT64_C(1)
#define MG_RENDER_STATS_COMMANDS UINT64_C(2)
#define MG_RENDER_STATS_MEMORY UINT64_C(4)
#define MG_RENDER_STATS_ENDPOINTS UINT64_C(8)
/* Byte counters cover this service's provider-image path, not Shell UI draws.
 * They are qualified only when all relevant native command counts are zero. */
#define MG_RENDER_STATS_TRANSFER_BYTES UINT64_C(16)
typedef struct {
    uint32_t struct_size, version; uint64_t available;
    uint64_t backing_allocations, backing_reuses, backing_live_bytes, backing_highwater_bytes;
    uint64_t clear_commands, copy_commands, readback_commands, upload_bytes, copy_bytes, readback_bytes;
    uint64_t acquire_sequence, publish_sequence, consume_sequence, complete_sequence, present_sequence;
    uint32_t surfaces, surface_limit, targets_inflight, target_limit, scenes_inflight, scene_limit;
    uint64_t committed_bytes, retiring_bytes;
} mg_render_stats_v1;
#define MG_RENDER_TRACE_ACQUIRE UINT32_C(1)
#define MG_RENDER_TRACE_PUBLISH UINT32_C(2)
#define MG_RENDER_TRACE_SCENE_ACCEPT UINT32_C(3)
#define MG_RENDER_TRACE_DISPATCH UINT32_C(4)
#define MG_RENDER_TRACE_NATIVE_COMPLETE UINT32_C(5)
#define MG_RENDER_TRACE_PRESENT UINT32_C(6)
typedef struct {
    uint64_t sequence, monotonic_ns; uint32_t phase, reserved;
    mg_render_key_v1 surface, content;
    uint64_t request_id, revision, submission, backing_allocations, backing_reuses;
} mg_render_trace_v1;
/* Destructive bounded owner read. Overflow is sticky; no overwritten samples
 * may be used as complete qualification evidence. All counts are actual. */
int32_t mg_render_trace_drain_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_trace_v1 *, uint32_t capacity, uint32_t *count, uint32_t *overflow);
int32_t mg_render_negotiate_v1_call(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_negotiate_v1 *, mg_render_capabilities_v1 *);
int32_t mg_render_surface_create_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_surface_config_v1 *, mg_render_key_v1 *);
int32_t mg_render_surface_configure_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_key_v1, const mg_render_surface_config_v1 *);
int32_t mg_render_surface_close_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_key_v1);
int32_t mg_render_target_acquire_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_acquire_v1 *, mg_render_target_v1 *);
int32_t mg_render_target_finish_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_finish_v1 *, mg_render_publication_v1 *);
int32_t mg_render_target_abandon_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_finish_v1 *);
int32_t mg_render_publication_close_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_key_v1 content);
/* Compositor-only, non-consuming SingleR selection. Returns newest ready
 * content only when the prior reader has no queued/native uses; otherwise
 * returns that exact retained reader (even after producer close). NOT_READY
 * means no eligible publication. Outputs are unchanged on failure. */
int32_t mg_render_publication_select_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_key_v1 surface, mg_render_publication_v1 *);
int32_t mg_render_scene_submit_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_scene_v1 *, uint64_t *revision);
int32_t mg_render_event_get_v1(mg_render_endpoint_v1 *, uint64_t epoch, uint64_t request_id, mg_render_event_v1 *);
int32_t mg_render_event_ack_v1(mg_render_endpoint_v1 *, uint64_t epoch, uint64_t request_id);
int32_t mg_render_hit_test_v1(mg_render_endpoint_v1 *, uint64_t epoch, const mg_render_hit_query_v1 *, mg_render_hit_v1 *);
int32_t mg_render_focus_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_key_v1 view, uint64_t focus_epoch);
int32_t mg_render_stats_snapshot_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_stats_v1 *);
typedef struct { uint32_t struct_size, version; uint64_t device_epoch; uint32_t phase; int32_t status; } mg_render_device_event_v1;
int32_t mg_render_device_event_get_v1(mg_render_endpoint_v1 *, uint64_t epoch, mg_render_device_event_v1 *);
#ifdef __cplusplus
#define MG_RENDER_ASSERT static_assert
#else
#define MG_RENDER_ASSERT _Static_assert
#endif
MG_RENDER_ASSERT(sizeof(mg_render_key_v1)==24, "render key layout");
MG_RENDER_ASSERT(sizeof(mg_render_surface_config_v1)==32, "surface config layout");
MG_RENDER_ASSERT(sizeof(mg_render_target_v1)==240 && offsetof(mg_render_target_v1,transfer_name)==112, "target layout");
MG_RENDER_ASSERT(sizeof(mg_render_view_v1)==224, "view layout");
MG_RENDER_ASSERT(sizeof(mg_render_event_v1)==56, "event layout");
MG_RENDER_ASSERT(sizeof(mg_render_trace_v1)==112, "trace layout");
MG_RENDER_ASSERT(sizeof(mg_render_writer_v1)==32 && sizeof(mg_render_write_mapping_v1)==32, "mapping layout");
#undef MG_RENDER_ASSERT
#ifdef __cplusplus
}
#endif
#endif
