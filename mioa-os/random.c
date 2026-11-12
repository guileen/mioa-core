#include "api/system/random_v1.h"

#include <stddef.h>
#include <stdint.h>

#if defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__)
#include <sys/random.h>
#include <unistd.h>
#elif defined(__linux__)
#include <sys/random.h>
#elif defined(_WIN32)
#include <windows.h>
#include <bcrypt.h>
#endif

mioa_random_status_v1 mioa_random_bytes_v1(void *buffer, size_t length) {
    if (length == 0) return MIOA_RANDOM_OK;
    if (!buffer) return MIOA_RANDOM_INVALID;

#if defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__)
    /* getentropy supports up to 256 bytes per call */
    uint8_t *ptr = (uint8_t *)buffer;
    while (length > 0) {
        size_t chunk = length > 256 ? 256 : length;
        if (getentropy(ptr, chunk) != 0) {
            return MIOA_RANDOM_UNAVAILABLE;
        }
        ptr += chunk;
        length -= chunk;
    }
    return MIOA_RANDOM_OK;
#elif defined(__linux__)
    uint8_t *ptr = (uint8_t *)buffer;
    while (length > 0) {
        ssize_t ret = getrandom(ptr, length, 0);
        if (ret < 0) return MIOA_RANDOM_UNAVAILABLE;
        ptr += ret;
        length -= (size_t)ret;
    }
    return MIOA_RANDOM_OK;
#elif defined(_WIN32)
    if (BCryptGenRandom(NULL, (PUCHAR)buffer, (ULONG)length, BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0) {
        return MIOA_RANDOM_OK;
    }
    return MIOA_RANDOM_UNAVAILABLE;
#else
    return MIOA_RANDOM_UNAVAILABLE;
#endif
}
