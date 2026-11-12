#ifndef MIOA_CLOCK_V1_H
#define MIOA_CLOCK_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_clock_status_v1 {
    MIOA_CLOCK_OK = 0,
    MIOA_CLOCK_INVALID = 1,
    MIOA_CLOCK_UNAVAILABLE = 2
} mioa_clock_status_v1;

/* Obtains current monotonic time in nanoseconds. Suitable for interval measurement. */
mioa_clock_status_v1 mioa_clock_monotonic_ns_v1(uint64_t *out_ns);

/* Obtains current realtime (wall clock / epoch) time in nanoseconds. */
mioa_clock_status_v1 mioa_clock_realtime_ns_v1(uint64_t *out_ns);

/* Suspends calling thread execution for at least the specified nanoseconds. */
mioa_clock_status_v1 mioa_clock_sleep_ns_v1(uint64_t nanoseconds);

#ifdef __cplusplus
}
#endif
#endif
