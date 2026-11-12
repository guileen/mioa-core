#include "api/system/environment_v1.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static int ascii_alpha(unsigned char value) {
    return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
}

static int valid_name(const char *name) {
    if (!name || !name[0] || !(ascii_alpha((unsigned char)name[0]) || name[0] == '_'))
        return 0;
    for (const unsigned char *p = (const unsigned char *)name + 1; *p; ++p) {
        if (!(ascii_alpha(*p) || (*p >= '0' && *p <= '9') || *p == '_'))
            return 0;
    }
    return 1;
}

typedef struct mioa_env_entry {
    char *name;
    char *value;
    size_t length;
    int is_unset;
    struct mioa_env_entry *next;
} mioa_env_entry;

static mioa_env_entry *g_virtual_env = NULL;

static mioa_env_entry *find_entry(const char *name) {
    for (mioa_env_entry *cur = g_virtual_env; cur; cur = cur->next) {
        if (strcmp(cur->name, name) == 0) return cur;
    }
    return NULL;
}

mioa_environment_status_v1 mioa_environment_get_v1(const char *name,
    mioa_environment_value_v1 *out) {
    if (!out || out->value || out->length || !valid_name(name))
        return MIOA_ENVIRONMENT_INVALID;

    mioa_env_entry *entry = find_entry(name);
    if (entry) {
        if (entry->is_unset) return MIOA_ENVIRONMENT_MISSING;
        char *copy = malloc(entry->length + 1);
        if (!copy) return MIOA_ENVIRONMENT_NO_MEMORY;
        memcpy(copy, entry->value, entry->length + 1);
        out->value = copy;
        out->length = entry->length;
        return MIOA_ENVIRONMENT_OK;
    }

    const char *current = getenv(name);
    if (!current) return MIOA_ENVIRONMENT_MISSING;

    size_t length = strlen(current);
    if (length == SIZE_MAX) return MIOA_ENVIRONMENT_NO_MEMORY;
    char *copy = malloc(length + 1);
    if (!copy) return MIOA_ENVIRONMENT_NO_MEMORY;
    memcpy(copy, current, length + 1);
    out->value = copy;
    out->length = length;
    return MIOA_ENVIRONMENT_OK;
}
const char *mioa_environment_get_raw_v1(const char *name) {
    if (!valid_name(name)) return NULL;
    mioa_env_entry *entry = find_entry(name);
    if (entry) {
        if (entry->is_unset) return NULL;
        return entry->value;
    }
    return getenv(name);
}

mioa_environment_status_v1 mioa_environment_has_v1(const char *name,
    int *present) {
    if (!present || !valid_name(name)) return MIOA_ENVIRONMENT_INVALID;
    mioa_env_entry *entry = find_entry(name);
    if (entry) {
        *present = !entry->is_unset;
        return MIOA_ENVIRONMENT_OK;
    }
    *present = getenv(name) != NULL;
    return MIOA_ENVIRONMENT_OK;
}

mioa_environment_status_v1 mioa_environment_set_v1(const char *name,
    const char *value, int overwrite) {
    if (!valid_name(name) || !value) return MIOA_ENVIRONMENT_INVALID;
    mioa_env_entry *entry = find_entry(name);
    if (entry) {
        if (!entry->is_unset && !overwrite) return MIOA_ENVIRONMENT_OK;
        size_t len = strlen(value);
        char *new_val = malloc(len + 1);
        if (!new_val) return MIOA_ENVIRONMENT_NO_MEMORY;
        memcpy(new_val, value, len + 1);
        free(entry->value);
        entry->value = new_val;
        entry->length = len;
        entry->is_unset = 0;
        return MIOA_ENVIRONMENT_OK;
    }

    size_t name_len = strlen(name);
    size_t val_len = strlen(value);
    char *name_copy = malloc(name_len + 1);
    char *val_copy = malloc(val_len + 1);
    mioa_env_entry *node = malloc(sizeof(mioa_env_entry));
    if (!name_copy || !val_copy || !node) {
        free(name_copy);
        free(val_copy);
        free(node);
        return MIOA_ENVIRONMENT_NO_MEMORY;
    }
    memcpy(name_copy, name, name_len + 1);
    memcpy(val_copy, value, val_len + 1);
    node->name = name_copy;
    node->value = val_copy;
    node->length = val_len;
    node->is_unset = 0;
    node->next = g_virtual_env;
    g_virtual_env = node;
    return MIOA_ENVIRONMENT_OK;
}

mioa_environment_status_v1 mioa_environment_unset_v1(const char *name) {
    if (!valid_name(name)) return MIOA_ENVIRONMENT_INVALID;
    mioa_env_entry *entry = find_entry(name);
    if (entry) {
        entry->is_unset = 1;
        free(entry->value);
        entry->value = NULL;
        entry->length = 0;
        return MIOA_ENVIRONMENT_OK;
    }
    size_t name_len = strlen(name);
    char *name_copy = malloc(name_len + 1);
    mioa_env_entry *node = malloc(sizeof(mioa_env_entry));
    if (!name_copy || !node) {
        free(name_copy);
        free(node);
        return MIOA_ENVIRONMENT_NO_MEMORY;
    }
    memcpy(name_copy, name, name_len + 1);
    node->name = name_copy;
    node->value = NULL;
    node->length = 0;
    node->is_unset = 1;
    node->next = g_virtual_env;
    g_virtual_env = node;
    return MIOA_ENVIRONMENT_OK;
}

void mioa_environment_clear_v1(void) {
    mioa_env_entry *cur = g_virtual_env;
    while (cur) {
        mioa_env_entry *next = cur->next;
        free(cur->name);
        free(cur->value);
        free(cur);
        cur = next;
    }
    g_virtual_env = NULL;
}


void mioa_environment_value_release_v1(mioa_environment_value_v1 *value) {
    if (!value) return;
    free(value->value);
    value->value = NULL;
    value->length = 0;
}
