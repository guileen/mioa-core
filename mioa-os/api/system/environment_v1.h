#ifndef MIOA_ENVIRONMENT_V1_H
#define MIOA_ENVIRONMENT_V1_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_environment_status_v1 {
    MIOA_ENVIRONMENT_OK = 0,
    MIOA_ENVIRONMENT_MISSING = 1,
    MIOA_ENVIRONMENT_INVALID = 2,
    MIOA_ENVIRONMENT_NO_MEMORY = 3
} mioa_environment_status_v1;

typedef struct mioa_environment_value_v1 {
    char *value;
    size_t length;
} mioa_environment_value_v1;

/* name is a nonempty ASCII identifier. out must be zero-initialized and is
 * unchanged unless OK is returned. Empty values are present values. On success,
 * value[length] is NUL and the caller owns the snapshot until release. */
mioa_environment_status_v1 mioa_environment_get_v1(const char *name,
    mioa_environment_value_v1 *out);
/* Presence-only lookup avoids copying the value. On OK, present is 1 even
 * for an empty value and 0 when absent. Invalid input leaves it unchanged. */
mioa_environment_status_v1 mioa_environment_has_v1(const char *name,
    int *present);
/* Set an environment variable in the virtual/injected environment table.
 * If overwrite is zero and name already exists, MIOA_ENVIRONMENT_OK is returned
 * without modifying the existing value. On invalid name or value, MIOA_ENVIRONMENT_INVALID. */
mioa_environment_status_v1 mioa_environment_set_v1(const char *name,
    const char *value, int overwrite);
/* Unset / remove an environment variable from the virtual/injected environment table. */
mioa_environment_status_v1 mioa_environment_unset_v1(const char *name);
/* Clear all virtual / injected environment variables. */
void mioa_environment_clear_v1(void);
void mioa_environment_value_release_v1(mioa_environment_value_v1 *value);
/* Returns a pointer to the value string (NUL-terminated) without copying,
 * or NULL if missing/unset/invalid. Caller must not free the returned pointer. */
const char *mioa_environment_get_raw_v1(const char *name);

#ifdef __cplusplus
}
#endif
#endif
