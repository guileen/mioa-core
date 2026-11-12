#define _POSIX_C_SOURCE 200809L
#include "files.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

_Static_assert(sizeof(off_t) >= 8, "file-v1 requires 64-bit off_t");
_Static_assert(sizeof(size_t) >= 8, "file-v1 requires 64-bit size_t");

#define FEATURES (MG_FILE_POSITIONAL | MG_FILE_REVOCATION)
#define MAX_SLOTS UINT32_C(65536)

struct file_slot {
    int fd;
    uint32_t generation, rights;
    uint64_t base, length;
    int revoked;
};
struct mg_file_endpoint_v1 {
    uint64_t epoch;
    uint32_t capacity;
    int negotiated;
    struct file_slot *slots;
};
struct mg_os_files {
    struct mg_file_endpoint_v1 endpoint;
    struct file_slot slots[];
};

static struct file_slot *lookup(mg_file_endpoint_v1 *ep, mg_file_handle_v1 h) {
    uint32_t index = (uint32_t)h.object;
    uint32_t generation = (uint32_t)(h.object >> 32);
    if (!ep || h.connection != ep->epoch || !index || index > ep->capacity || !generation)
        return NULL;
    struct file_slot *slot = &ep->slots[index - 1];
    return slot->fd >= 0 && slot->generation == generation ? slot : NULL;
}

mg_file_status_v1 mg_os_files_create(uint32_t capacity, uint64_t epoch,
    uint64_t required, mg_os_files **out, mg_file_endpoint_v1 **endpoint) {
    if (!out || !endpoint || !epoch || !capacity || capacity > MAX_SLOTS)
        return MG_FILE_INVALID;
    if (required & ~FEATURES) return MG_FILE_UNSUPPORTED;
    mg_os_files *owner = calloc(1, sizeof(*owner) + capacity * sizeof(struct file_slot));
    if (!owner) return MG_FILE_LIMIT;
    owner->endpoint.slots = owner->slots;
    owner->endpoint.epoch = epoch;
    owner->endpoint.capacity = capacity;
    for (uint32_t i = 0; i < capacity; ++i) owner->endpoint.slots[i].fd = -1;
    *out = owner;
    *endpoint = &owner->endpoint;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_file_negotiate_v1(mg_file_endpoint_v1 *ep,
    uint32_t version, uint64_t required, uint64_t *selected) {
    if (!ep || !selected) return MG_FILE_INVALID;
    if (version != MG_FILE_V1_VERSION || (required & ~FEATURES)) return MG_FILE_UNSUPPORTED;
    ep->negotiated = 1;
    *selected = FEATURES;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_os_files_grant(mg_os_files *owner, int source,
    uint32_t rights, uint64_t base, uint64_t length, mg_file_handle_v1 *out) {
    if (!owner || !out || source < 0 || !rights || (rights & ~(MG_FILE_READ | MG_FILE_WRITE)) ||
        base > INT64_MAX || length > (uint64_t)INT64_MAX - base) return MG_FILE_INVALID;
    mg_file_endpoint_v1 *ep = &owner->endpoint;
    uint32_t index = 0;
    while (index < ep->capacity &&
        (ep->slots[index].fd >= 0 || ep->slots[index].generation == UINT32_MAX)) ++index;
    if (index == ep->capacity) return MG_FILE_LIMIT;
    int fd = fcntl(source, F_DUPFD_CLOEXEC, 0);
    if (fd < 0) return errno == EMFILE || errno == ENFILE ? MG_FILE_LIMIT : MG_FILE_IO;
    struct stat metadata;
    int flags = fcntl(fd, F_GETFL);
    mg_file_status_v1 status = MG_FILE_OK;
    if (flags < 0 || fstat(fd, &metadata) < 0) status = MG_FILE_IO;
    else if (!S_ISREG(metadata.st_mode) || (flags & O_APPEND)) status = MG_FILE_UNSUPPORTED;
    else if (((rights & MG_FILE_READ) && (flags & O_ACCMODE) == O_WRONLY) ||
             ((rights & MG_FILE_WRITE) && (flags & O_ACCMODE) == O_RDONLY)) status = MG_FILE_DENIED;
    if (status != MG_FILE_OK) { close(fd); return status; }
    struct file_slot *slot = &ep->slots[index];
    ++slot->generation;
    slot->fd = fd;
    slot->rights = rights;
    slot->base = base;
    slot->length = length;
    slot->revoked = 0;
    *out = (mg_file_handle_v1){ep->epoch, ((uint64_t)slot->generation << 32) | (index + 1)};
    return MG_FILE_OK;
}

static mg_file_status_v1 access_range(mg_file_endpoint_v1 *ep, mg_file_handle_v1 h,
    uint32_t right, uint64_t offset, const void *buffer, uint64_t length,
    uint64_t *count, struct file_slot **out) {
    if (!ep || !count || (length && !buffer)) return MG_FILE_INVALID;
    if (!ep->negotiated) return MG_FILE_UNSUPPORTED;
    struct file_slot *slot = lookup(ep, h);
    if (!slot) return MG_FILE_STALE;
    if (slot->revoked || !(slot->rights & right)) return MG_FILE_DENIED;
    if (length > MG_FILE_V1_MAX_TRANSFER) return MG_FILE_LIMIT;
    if (offset > slot->length || length > slot->length - offset) return MG_FILE_INVALID;
    *out = slot;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_file_read_v1(mg_file_endpoint_v1 *ep, mg_file_handle_v1 h,
    uint64_t offset, void *buffer, uint64_t length, uint64_t *count) {
    struct file_slot *slot;
    mg_file_status_v1 status = access_range(ep, h, MG_FILE_READ, offset, buffer, length, count, &slot);
    if (status != MG_FILE_OK) return status;
    if (!length) { *count = 0; return MG_FILE_OK; }
    ssize_t result;
    do { result = pread(slot->fd, buffer, (size_t)length, (off_t)(slot->base + offset)); }
    while (result < 0 && errno == EINTR);
    if (result < 0) return MG_FILE_IO;
    *count = (uint64_t)result;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_file_write_v1(mg_file_endpoint_v1 *ep, mg_file_handle_v1 h,
    uint64_t offset, const void *buffer, uint64_t length, uint64_t *count) {
    struct file_slot *slot;
    mg_file_status_v1 status = access_range(ep, h, MG_FILE_WRITE, offset, buffer, length, count, &slot);
    if (status != MG_FILE_OK) return status;
    if (!length) { *count = 0; return MG_FILE_OK; }
    ssize_t result;
    do { result = pwrite(slot->fd, buffer, (size_t)length, (off_t)(slot->base + offset)); }
    while (result < 0 && errno == EINTR);
    if (result < 0) return MG_FILE_IO;
    *count = (uint64_t)result;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_os_files_revoke(mg_os_files *owner, mg_file_handle_v1 h) {
    struct file_slot *slot = lookup(owner ? &owner->endpoint : NULL, h);
    if (!slot) return MG_FILE_STALE;
    slot->revoked = 1;
    return MG_FILE_OK;
}

mg_file_status_v1 mg_file_close_v1(mg_file_endpoint_v1 *ep, mg_file_handle_v1 h) {
    struct file_slot *slot = lookup(ep, h);
    if (!slot) return MG_FILE_STALE;
    int fd = slot->fd;
    slot->fd = -1;
    return close(fd) == 0 ? MG_FILE_OK : MG_FILE_IO;
}

mg_file_status_v1 mg_os_files_destroy(mg_os_files *owner) {
    if (!owner) return MG_FILE_INVALID;
    mg_file_status_v1 status = MG_FILE_OK;
    for (uint32_t i = 0; i < owner->endpoint.capacity; ++i) {
        int fd = owner->endpoint.slots[i].fd;
        if (fd >= 0 && close(fd) < 0) status = MG_FILE_IO;
    }
    free(owner);
    return status;
}

bool mg_os_raw_frame_write(const char *path_bytes, size_t path_len, uint32_t slot,
                          const uint8_t *bytes, size_t len, uint64_t capacity) {
    if (!path_bytes || !path_len || path_len > 4096 || !bytes || !len || slot > 2) return false;
    char path[4097];
    memcpy(path, path_bytes, path_len);
    path[path_len] = '\0';
    if (path[0] != '/') return false;

    int nofollow = 0;
#if defined(O_NOFOLLOW)
    nofollow = O_NOFOLLOW;
#endif

    int fd = open(path, O_RDWR | nofollow);
    if (fd < 0) return false;

    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || (st.st_mode & 0777) != 0600) {
        close(fd);
        return false;
    }

    char parent[4097];
    size_t last_slash = 0;
    for (size_t i = 0; i < path_len; ++i) {
        if (path[i] == '/') last_slash = i;
    }
    if (last_slash == 0) {
        parent[0] = '/';
        parent[1] = '\0';
    } else {
        memcpy(parent, path, last_slash);
        parent[last_slash] = '\0';
    }
    struct stat parent_st;
    if (lstat(parent, &parent_st) != 0 || !S_ISDIR(parent_st.st_mode) ||
        (parent_st.st_mode & 0777) != 0700 || parent_st.st_uid != st.st_uid ||
        (uint64_t)st.st_size != (64 + 3 * (64 + capacity))) {
        close(fd);
        return false;
    }

    uint8_t header[16];
    ssize_t n = pread(fd, header, 16, 0);
    if (n != 16) {
        close(fd);
        return false;
    }
    uint32_t magic = (uint32_t)header[0] | ((uint32_t)header[1] << 8) |
                     ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 24);
    uint16_t version = (uint16_t)header[4] | ((uint16_t)header[5] << 8);
    uint32_t cap = (uint32_t)header[12] | ((uint32_t)header[13] << 8) |
                   ((uint32_t)header[14] << 16) | ((uint32_t)header[15] << 24);
    if (magic != 0x4c504652 || version != 2 || (uint64_t)cap != capacity) {
        close(fd);
        return false;
    }

    off_t offset = (off_t)(64 + (uint64_t)slot * (64 + capacity) + 64);
    ssize_t written = pwrite(fd, bytes, len, offset);
    close(fd);
    return written == (ssize_t)len;
}
