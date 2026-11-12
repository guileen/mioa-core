/* SPDX-License-Identifier: MIT */
/* Private trusted-executor client for the graphics resource broker. */
#include "api/system/broker_v1.h"

#ifdef __APPLE__
#include <CoreFoundation/CoreFoundation.h>
#include <IOSurface/IOSurface.h>
#include <bsm/libbsm.h>
#include <mach/mach.h>
#include <servers/bootstrap.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

#define REQUEST_ID 0x4d470101
#define REPLY_ID 0x4d470102

typedef struct { mach_msg_header_t header; uint64_t epoch; } request;
typedef struct { mach_msg_header_t header; mach_msg_body_t body; mach_msg_port_descriptor_t surface; uint64_t epoch; } reply;
typedef bool (*paint_fn)(void *, uint8_t *, size_t, size_t);
typedef mioa_backing_metadata_v1 backing_metadata;

enum broker_diag_stage {
    DIAG_ENTRY=1, DIAG_ARGUMENTS, DIAG_SPAN, DIAG_BOOTSTRAP,
    DIAG_RESPONSE_ALLOCATE, DIAG_SEND, DIAG_RECEIVE, DIAG_RESPONSE_RELEASE,
    DIAG_REPLY, DIAG_SURFACE_LOOKUP, DIAG_SURFACE_RELEASE,
    DIAG_WIDTH, DIAG_HEIGHT, DIAG_ELEMENT, DIAG_FORMAT, DIAG_PLANES,
    DIAG_LAYOUT, DIAG_LOCK, DIAG_BASE, DIAG_PAINT, DIAG_UNLOCK
};
static atomic_uint broker_diag_count = 0;
static void broker_diag(unsigned stage, int64_t code) {
    unsigned count = atomic_load_explicit(&broker_diag_count, memory_order_relaxed);
    for (unsigned attempt=0; attempt<16 && count<8; ++attempt) {
        if (!atomic_compare_exchange_weak_explicit(&broker_diag_count, &count, count+1,
                memory_order_relaxed, memory_order_relaxed)) continue;
        const int saved_errno = errno;
        struct stat status;
        bool safe = fstat(STDERR_FILENO, &status)==0 &&
            (S_ISREG(status.st_mode) || S_ISCHR(status.st_mode));
        if (!safe) {
            struct sigaction action;
            safe = sigaction(SIGPIPE, NULL, &action)==0 && action.sa_handler==SIG_IGN;
        }
        if (safe) {
            char line[128];
            int length=snprintf(line,sizeof(line),"mioa_broker_diag stage=%u code=%" PRId64 "\n",stage,code);
            if (length<=0 || (size_t)length>=sizeof(line) ||
                write(STDERR_FILENO,line,(size_t)length)!=length)
                atomic_store_explicit(&broker_diag_count,8,memory_order_relaxed);
        } else atomic_store_explicit(&broker_diag_count,8,memory_order_relaxed);
        errno=saved_errno;
        return;
    }
}

/* The trusted launcher pins the Host process generation and the native server
 * admits only its kernel-authenticated Unix CDP peer. The Mach reply must come
 * from that same live Host generation, independently of the rendezvous name. */
static uint32_t paint_with_host(const char *name, uint64_t epoch, int32_t host, uint32_t uid,
                            uint32_t timeout, uint32_t width, uint32_t height,
                            void *context, paint_fn paint, int version, backing_metadata *metadata) {
    if (!name || !*name || strlen(name) >= 128 || !epoch || host <= 0 ||
        !timeout || !width || !height || !paint || !metadata || (size_t)width > SIZE_MAX / 4 ||
        (size_t)width * 4 > SIZE_MAX / height) { broker_diag(DIAG_ARGUMENTS,0); return false; }
    const size_t pixel_row = (size_t)width * 4;
    if ((uint64_t)width * height > (uint64_t)3840 * 2160) { broker_diag(DIAG_SPAN,0); return false; }
    mach_port_t service = MACH_PORT_NULL, response = MACH_PORT_NULL;
    kern_return_t lookup=bootstrap_look_up(bootstrap_port,name,&service);
    if (lookup!=KERN_SUCCESS) { broker_diag(DIAG_BOOTSTRAP,lookup); return false; }
    kern_return_t allocated=mach_port_allocate(mach_task_self(),MACH_PORT_RIGHT_RECEIVE,&response);
    if (allocated!=KERN_SUCCESS) {
        mach_port_deallocate(mach_task_self(), service); broker_diag(DIAG_RESPONSE_ALLOCATE,allocated); return false;
    }
    request q = {0};
    q.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND_ONCE);
    q.header.msgh_size = sizeof(q); q.header.msgh_remote_port = service;
    q.header.msgh_local_port = response; q.header.msgh_id = REQUEST_ID; q.epoch = epoch;
    mach_msg_return_t rc = mach_msg(&q.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                                    sizeof(q), 0, MACH_PORT_NULL, timeout, MACH_PORT_NULL);
    mach_port_deallocate(mach_task_self(), service);
    if (rc != MACH_MSG_SUCCESS) {
        (void)mach_port_mod_refs(mach_task_self(), response, MACH_PORT_RIGHT_RECEIVE, -1);
        broker_diag(DIAG_SEND,rc);
        return false;
    }
    struct { reply message; mach_msg_max_trailer_t trailer; } buf = {0};
    rc = mach_msg(&buf.message.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT |
                  MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0) |
                  MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_AUDIT), 0,
                  sizeof(buf), response, timeout, MACH_PORT_NULL);
    kern_return_t response_released = mach_port_mod_refs(mach_task_self(), response, MACH_PORT_RIGHT_RECEIVE, -1);
    if (rc != MACH_MSG_SUCCESS) { broker_diag(DIAG_RECEIVE,rc); return false; }
    if (response_released != KERN_SUCCESS) {
        mach_msg_destroy(&buf.message.header);
        broker_diag(DIAG_RESPONSE_RELEASE,response_released);
        return false;
    }
    reply *r = &buf.message;
    mach_msg_audit_trailer_t *trailer = (void *)((char *)r + ((r->header.msgh_size + 3u) & ~3u));
    if (r->header.msgh_size != sizeof(*r) || r->header.msgh_id != REPLY_ID ||
        !(r->header.msgh_bits & MACH_MSGH_BITS_COMPLEX) || r->body.msgh_descriptor_count != 1 ||
        r->surface.type != MACH_MSG_PORT_DESCRIPTOR || r->surface.disposition != MACH_MSG_TYPE_PORT_SEND ||
        !MACH_PORT_VALID(r->surface.name) || r->epoch != epoch ||
        trailer->msgh_trailer_type != MACH_MSG_TRAILER_FORMAT_0 || trailer->msgh_trailer_size < sizeof(*trailer) ||
        audit_token_to_pid(trailer->msgh_audit) != host || audit_token_to_euid(trailer->msgh_audit) != uid ||
        audit_token_to_pidversion(trailer->msgh_audit) != version) {
        mach_msg_destroy(&r->header); broker_diag(DIAG_REPLY,0); return false;
    }
    mach_port_t port = r->surface.name;
    IOSurfaceRef surface = IOSurfaceLookupFromMachPort(port);
    kern_return_t released = mach_port_deallocate(mach_task_self(), port);
    if (!surface) { broker_diag(DIAG_SURFACE_LOOKUP,0); return false; }
    if (released != KERN_SUCCESS) { CFRelease(surface); broker_diag(DIAG_SURFACE_RELEASE,released); return false; }
    uint32_t result = 0;
    const size_t row = IOSurfaceGetBytesPerRow(surface);
    const size_t allocation = IOSurfaceGetAllocSize(surface);
    const bool bounded = row >= pixel_row && row % 4 == 0 &&
        row <= UINT32_MAX && row <= SIZE_MAX / height && row * height <= allocation;
    const size_t len = bounded ? row * height : 0;
    unsigned failure_stage=DIAG_WIDTH;
    IOReturn locked=kIOReturnSuccess;
    const bool geometry_matches=IOSurfaceGetWidth(surface)==width &&
        (failure_stage=DIAG_HEIGHT, IOSurfaceGetHeight(surface)==height);
    if (geometry_matches &&
        (failure_stage=DIAG_ELEMENT, IOSurfaceGetBytesPerElement(surface) == 4) &&
        (failure_stage=DIAG_FORMAT, IOSurfaceGetPixelFormat(surface) == 0x52474241) &&
        (failure_stage=DIAG_PLANES, IOSurfaceGetPlaneCount(surface) == 0) &&
        (failure_stage=DIAG_LAYOUT, bounded) &&
        (failure_stage=DIAG_LOCK, locked=IOSurfaceLock(surface,0,NULL), locked==kIOReturnSuccess)) {
        uint8_t *base = IOSurfaceGetBaseAddress(surface);
        /* Strict synchronous borrow: paint must not retain base or enqueue GPU
         * writes. No surface or Mach right escapes this scope. Only the pinned,
         * trusted Core CPU executor profile may treat our success as release. */
        const bool painted = base && paint(context, base, len, row);
        if (!base) broker_diag(DIAG_BASE,0);
        else if (!painted) broker_diag(DIAG_PAINT,0);
        IOReturn unlocked=IOSurfaceUnlock(surface,0,NULL);
        if (unlocked == kIOReturnSuccess) {
            /* Safe borrow completion is distinct from stable/complete pixels.
             * No success bit reaches the caller until CFRelease below. */
            result = 1u | (painted ? 2u : 0u);
            *metadata = (backing_metadata){row, len, allocation};
        }
        else broker_diag(DIAG_UNLOCK,unlocked);
    }
    else broker_diag(failure_stage,failure_stage==DIAG_LOCK ? (int64_t)locked : 0);
    CFRelease(surface);
    if (!geometry_matches) {
        /* No lock, base-address access or paint occurred. All received rights
         * and this Surface reference are now released; never claim pixels. */
        *metadata=(backing_metadata){0};
        return 1u;
    }
    return result;
}

uint32_t lp_broker_paint(const char *name, uint64_t epoch, int32_t host, uint32_t uid,
                     int32_t version, uint32_t timeout, uint32_t width, uint32_t height,
                     void *context, paint_fn paint, backing_metadata *metadata) {
    if (!epoch || host <= 0 || !timeout) { broker_diag(DIAG_ENTRY,0); return false; }
    return paint_with_host(name, epoch, host, uid, timeout, width, height, context, paint, version, metadata);
}

bool lp_trusted_cdp_peer(int fd, int32_t host, uint32_t uid, int32_t version) {
    audit_token_t token;
    socklen_t size = sizeof(token);
    return getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &size) == 0 &&
           size == sizeof(token) && audit_token_to_pid(token) == host &&
           audit_token_to_euid(token) == uid && audit_token_to_pidversion(token) == version;
}
#else
uint32_t lp_broker_paint(const char *name, uint64_t epoch, int32_t host, uint32_t uid,
                     int32_t version, uint32_t timeout, uint32_t width, uint32_t height,
                     void *context, paint_fn paint, backing_metadata *metadata) {
    (void)name; (void)epoch; (void)host; (void)uid; (void)version;
    (void)timeout; (void)width; (void)height; (void)context; (void)paint; (void)metadata;
    return 0;
}

bool lp_trusted_cdp_peer(int fd, int32_t host, uint32_t uid, int32_t version) {
    (void)fd; (void)host; (void)uid; (void)version;
    return true;
}
#endif
