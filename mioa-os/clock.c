#include "api/system/clock_v1.h"

#include <time.h>
#if defined(_WIN32)
#include <windows.h>
#else
#include <errno.h>
#endif

mioa_clock_status_v1 mioa_clock_monotonic_ns_v1(uint64_t *out_ns) {
    if (!out_ns) return MIOA_CLOCK_INVALID;
#if defined(_WIN32)
    LARGE_INTEGER freq, counter;
    if (!QueryPerformanceFrequency(&freq) || !QueryPerformanceCounter(&counter))
        return MIOA_CLOCK_UNAVAILABLE;
    *out_ns = (uint64_t)((counter.QuadPart * 1000000000ULL) / freq.QuadPart);
    return MIOA_CLOCK_OK;
#else
    struct timespec ts;
#if defined(CLOCK_MONOTONIC_RAW)
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts) == 0) {
        *out_ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
        return MIOA_CLOCK_OK;
    }
#endif
    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
        *out_ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
        return MIOA_CLOCK_OK;
    }
    return MIOA_CLOCK_UNAVAILABLE;
#endif
}

mioa_clock_status_v1 mioa_clock_realtime_ns_v1(uint64_t *out_ns) {
    if (!out_ns) return MIOA_CLOCK_INVALID;
#if defined(_WIN32)
    FILETIME ft;
    GetSystemTimePreciseAsFileTime(&ft);
    ULARGE_INTEGER uli;
    uli.LowPart = ft.dwLowDateTime;
    uli.HighPart = ft.dwHighDateTime;
    *out_ns = (uli.QuadPart - 116444736000000000ULL) * 100ULL;
    return MIOA_CLOCK_OK;
#else
    struct timespec ts;
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0) {
        *out_ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
        return MIOA_CLOCK_OK;
    }
    return MIOA_CLOCK_UNAVAILABLE;
#endif
}

mioa_clock_status_v1 mioa_clock_sleep_ns_v1(uint64_t nanoseconds) {
#if defined(_WIN32)
    DWORD ms = (DWORD)(nanoseconds / 1000000ULL);
    Sleep(ms);
    return MIOA_CLOCK_OK;
#else
    struct timespec ts;
    ts.tv_sec = (time_t)(nanoseconds / 1000000000ULL);
    ts.tv_nsec = (long)(nanoseconds % 1000000000ULL);
    while (nanosleep(&ts, &ts) == -1) {
        if (errno != EINTR) return MIOA_CLOCK_UNAVAILABLE;
    }
    return MIOA_CLOCK_OK;
#endif
}
