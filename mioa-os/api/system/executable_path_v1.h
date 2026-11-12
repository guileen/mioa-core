#ifndef MIOA_EXECUTABLE_PATH_V1_H
#define MIOA_EXECUTABLE_PATH_V1_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_executable_path_status_v1 {
    MIOA_EXECUTABLE_PATH_OK = 0,
    MIOA_EXECUTABLE_PATH_INVALID = 1,
    MIOA_EXECUTABLE_PATH_UNSUPPORTED = 2,
    MIOA_EXECUTABLE_PATH_NO_MEMORY = 3,
    MIOA_EXECUTABLE_PATH_IO = 4
} mioa_executable_path_status_v1;

typedef struct mioa_executable_path_value_v1 {
    char *path;
    size_t length;
} mioa_executable_path_value_v1;

/* Returns the running executable's openable UTF-8 path as an owned snapshot.
 * out must be zero-initialized and remains unchanged unless OK is returned.
 * Release successful results with mioa_executable_path_value_release_v1. */
mioa_executable_path_status_v1 mioa_executable_path_get_v1(
    mioa_executable_path_value_v1 *out);
void mioa_executable_path_value_release_v1(mioa_executable_path_value_v1 *value);

#ifdef __cplusplus
}
#endif
#endif
