#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include "api/system/executable_path_v1.h"

#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#elif defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#elif defined(__linux__)
#include <unistd.h>
#endif

static mioa_executable_path_status_v1 read_executable_path(
    mioa_executable_path_value_v1 *out) {
#if defined(__APPLE__)
    uint32_t capacity = 256;
    for (;;) {
        char *path = malloc((size_t)capacity);
        if (!path) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        uint32_t required = capacity;
        if (_NSGetExecutablePath(path, &required) == 0) {
            size_t length = strnlen(path, capacity);
            if (length == 0 || length == capacity) {
                free(path);
                return MIOA_EXECUTABLE_PATH_IO;
            }
            out->path = path;
            out->length = length;
            return MIOA_EXECUTABLE_PATH_OK;
        }
        free(path);
        if (required <= capacity) {
            if (capacity > UINT32_MAX / 2) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
            required = capacity * 2;
        }
        if (required <= capacity) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        capacity = required;
    }
#elif defined(_WIN32)
    DWORD capacity = 256;
    for (;;) {
        if ((size_t)capacity > SIZE_MAX / sizeof(wchar_t))
            return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        wchar_t *wide = malloc((size_t)capacity * sizeof(wchar_t));
        if (!wide) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        DWORD length = GetModuleFileNameW(NULL, wide, capacity);
        if (length == 0) {
            free(wide);
            return MIOA_EXECUTABLE_PATH_IO;
        }
        if (length >= capacity) {
            free(wide);
            if (capacity > MAXDWORD / 2) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
            capacity *= 2;
            continue;
        }
        if (length > INT_MAX) {
            free(wide);
            return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        }
        int utf8_length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            wide, (int)length, NULL, 0, NULL, NULL);
        if (utf8_length <= 0) {
            free(wide);
            return MIOA_EXECUTABLE_PATH_IO;
        }
        char *path = malloc((size_t)utf8_length + 1);
        if (!path) {
            free(wide);
            return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        }
        int converted = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            wide, (int)length, path, utf8_length, NULL, NULL);
        free(wide);
        if (converted != utf8_length) {
            free(path);
            return MIOA_EXECUTABLE_PATH_IO;
        }
        path[utf8_length] = '\0';
        out->path = path;
        out->length = (size_t)utf8_length;
        return MIOA_EXECUTABLE_PATH_OK;
    }
#elif defined(__linux__)
    size_t capacity = 256;
    for (;;) {
        if (capacity == SIZE_MAX) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        char *path = malloc(capacity + 1);
        if (!path) return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        ssize_t length = readlink("/proc/self/exe", path, capacity);
        if (length < 0) {
            free(path);
            return MIOA_EXECUTABLE_PATH_IO;
        }
        if ((size_t)length < capacity) {
            if (length == 0) {
                free(path);
                return MIOA_EXECUTABLE_PATH_IO;
            }
            path[length] = '\0';
            out->path = path;
            out->length = (size_t)length;
            return MIOA_EXECUTABLE_PATH_OK;
        }
        free(path);
        if (capacity > (SIZE_MAX - 1) / 2)
            return MIOA_EXECUTABLE_PATH_NO_MEMORY;
        capacity *= 2;
    }
#else
    (void)out;
    return MIOA_EXECUTABLE_PATH_UNSUPPORTED;
#endif
}

mioa_executable_path_status_v1 mioa_executable_path_get_v1(
    mioa_executable_path_value_v1 *out) {
    if (!out || out->path || out->length) return MIOA_EXECUTABLE_PATH_INVALID;
    return read_executable_path(out);
}

void mioa_executable_path_value_release_v1(mioa_executable_path_value_v1 *value) {
    if (!value) return;
    free(value->path);
    value->path = NULL;
    value->length = 0;
}
