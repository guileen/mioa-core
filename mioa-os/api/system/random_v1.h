#ifndef MIOA_RANDOM_V1_H
#define MIOA_RANDOM_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_random_status_v1 {
    MIOA_RANDOM_OK = 0,
    MIOA_RANDOM_INVALID = 1,
    MIOA_RANDOM_UNAVAILABLE = 2
} mioa_random_status_v1;

/* Fills buffer[0..length] with cryptographically secure random bytes from Host OS.
 * On success returns MIOA_RANDOM_OK.
 * Length 0 is a no-op returning MIOA_RANDOM_OK. Null buffer with nonzero length returns INVALID. */
mioa_random_status_v1 mioa_random_bytes_v1(void *buffer, size_t length);

#ifdef __cplusplus
}
#endif
#endif
