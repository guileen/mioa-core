#ifndef MIOA_APP_DATA_V1_H
#define MIOA_APP_DATA_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MIOA_APP_DATA_CREATE_DIRECTORY_V1 UINT32_C(1)

typedef enum mioa_app_data_status_v1 {
    MIOA_APP_DATA_OK = 0,
    MIOA_APP_DATA_INVALID = 1,
    MIOA_APP_DATA_UNAVAILABLE = 2,
    MIOA_APP_DATA_NO_MEMORY = 3,
    MIOA_APP_DATA_IO = 4
} mioa_app_data_status_v1;

typedef struct mioa_app_data_path_v1 {
    char *path;
    size_t length;
} mioa_app_data_path_v1;

/* app_name is one nonempty path component. options accepts zero or CREATE.
 * When CREATE is set, missing path components are created before success.
 * out must be zero-initialized and is unchanged on failure. The returned path
 * is caller-owned until release; bytes are UTF-8 on Windows and native on POSIX. */
mioa_app_data_status_v1 mioa_app_data_directory_v1(const char *app_name,
    uint32_t options, mioa_app_data_path_v1 *out);
void mioa_app_data_path_release_v1(mioa_app_data_path_v1 *path);

#ifdef __cplusplus
}
#endif
#endif
