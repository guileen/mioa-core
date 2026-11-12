#define _POSIX_C_SOURCE 200809L
#include "api/system/app_data_v1.h"

#include <ctype.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include <wctype.h>
#else
#include <sys/stat.h>
#endif

static int valid_app_name(const char *name) {
    if (!name || !name[0] || strcmp(name, ".") == 0 || strcmp(name, "..") == 0)
        return 0;
#ifdef _WIN32
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, NULL, 0))
        return 0;
#endif
    for (const unsigned char *p = (const unsigned char *)name; *p; ++p) {
        if (*p < 0x20 || *p == '/' || *p == '\\') return 0;
#ifdef _WIN32
        if (strchr("<>:\"|?*", *p) || (*p >= 0x80 && *p < 0xC2)) return 0;
#endif
    }
#ifdef _WIN32
    size_t length = strlen(name);
    if (name[length - 1] == '.' || name[length - 1] == ' ') return 0;
#endif
    return 1;
}

static char *join_path(const char *base, const char *leaf, char separator) {
    size_t base_length = strlen(base);
    const size_t leaf_length = strlen(leaf);
    while (base_length > 1 && (base[base_length - 1] == '/' || base[base_length - 1] == '\\'))
        --base_length;
    const int needs_separator = base_length == 0 ||
        (base[base_length - 1] != '/' && base[base_length - 1] != '\\');
    const size_t separator_length = (size_t)needs_separator;
    if (base_length > SIZE_MAX - separator_length - 1 ||
        leaf_length > SIZE_MAX - base_length - separator_length - 1)
        return NULL;
    const size_t length = base_length + separator_length + leaf_length;
    char *path = malloc(length + 1);
    if (!path) return NULL;
    memcpy(path, base, base_length);
    size_t offset = base_length;
    if (needs_separator) path[offset++] = separator;
    memcpy(path + offset, leaf, leaf_length);
    path[length] = '\0';
    return path;
}

#ifdef _WIN32
static mioa_app_data_status_v1 local_app_data(char **out) {
    const DWORD required = GetEnvironmentVariableW(L"LOCALAPPDATA", NULL, 0);
    if (!required) return MIOA_APP_DATA_UNAVAILABLE;
    if ((size_t)required > SIZE_MAX / sizeof(wchar_t)) return MIOA_APP_DATA_NO_MEMORY;
    wchar_t *wide = malloc((size_t)required * sizeof(*wide));
    if (!wide) return MIOA_APP_DATA_NO_MEMORY;
    const DWORD written = GetEnvironmentVariableW(L"LOCALAPPDATA", wide, required);
    if (!written || written >= required) {
        free(wide);
        return MIOA_APP_DATA_UNAVAILABLE;
    }
    const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
        wide, -1, NULL, 0, NULL, NULL);
    if (!bytes) {
        free(wide);
        return MIOA_APP_DATA_UNAVAILABLE;
    }
    char *utf8 = malloc((size_t)bytes);
    if (!utf8) {
        free(wide);
        return MIOA_APP_DATA_NO_MEMORY;
    }
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            wide, -1, utf8, bytes, NULL, NULL)) {
        free(utf8);
        free(wide);
        return MIOA_APP_DATA_UNAVAILABLE;
    }
    free(wide);
    if (!(isalpha((unsigned char)utf8[0]) && utf8[1] == ':' &&
            (utf8[2] == '/' || utf8[2] == '\\')) &&
        !(utf8[0] == '\\' && utf8[1] == '\\')) {
        free(utf8);
        return MIOA_APP_DATA_UNAVAILABLE;
    }
    *out = utf8;
    return MIOA_APP_DATA_OK;
}

static wchar_t *utf8_to_wide(const char *value) {
    const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        value, -1, NULL, 0);
    if (!count || (size_t)count > SIZE_MAX / sizeof(wchar_t)) return NULL;
    wchar_t *wide = malloc((size_t)count * sizeof(*wide));
    if (!wide) return NULL;
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
            value, -1, wide, count)) {
        free(wide);
        return NULL;
    }
    return wide;
}

static int is_separator(wchar_t c) { return c == L'\\' || c == L'/'; }

static mioa_app_data_status_v1 ensure_directory(const char *path) {
    wchar_t *wide = utf8_to_wide(path);
    if (!wide) return MIOA_APP_DATA_INVALID;
    const size_t length = wcslen(wide);
    size_t start;
    if (length >= 3 && iswalpha(wide[0]) && wide[1] == L':' && is_separator(wide[2])) {
        start = 3;
    } else if (length >= 5 && is_separator(wide[0]) && is_separator(wide[1])) {
        wchar_t *server_end = wcschr(wide + 2, L'\\');
        if (!server_end) server_end = wcschr(wide + 2, L'/');
        if (!server_end || !server_end[1]) { free(wide); return MIOA_APP_DATA_INVALID; }
        wchar_t *share_end = wcschr(server_end + 1, L'\\');
        if (!share_end) share_end = wcschr(server_end + 1, L'/');
        start = share_end ? (size_t)(share_end - wide) + 1 : length;
    } else {
        free(wide);
        return MIOA_APP_DATA_INVALID;
    }
    for (size_t i = start; i <= length; ++i) {
        if (i != length && !is_separator(wide[i])) continue;
        if (i == start) continue;
        const wchar_t saved = wide[i];
        wide[i] = L'\0';
        if (!CreateDirectoryW(wide, NULL)) {
            const DWORD error = GetLastError();
            const DWORD attributes = error == ERROR_ALREADY_EXISTS ? GetFileAttributesW(wide) : INVALID_FILE_ATTRIBUTES;
            if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) {
                wide[i] = saved;
                free(wide);
                return MIOA_APP_DATA_IO;
            }
        }
        wide[i] = saved;
    }
    free(wide);
    return MIOA_APP_DATA_OK;
}
#else
static mioa_app_data_status_v1 ensure_directory(const char *path) {
    if (!path || path[0] != '/') return MIOA_APP_DATA_INVALID;
    const size_t length = strlen(path);
    char *prefix = malloc(length + 1);
    if (!prefix) return MIOA_APP_DATA_NO_MEMORY;
    memcpy(prefix, path, length + 1);
    for (size_t i = 1; i <= length; ++i) {
        if (i != length && prefix[i] != '/') continue;
        if (i == 1) continue;
        const char saved = prefix[i];
        prefix[i] = '\0';
        if (mkdir(prefix, 0700) != 0) {
            struct stat metadata;
            if (errno != EEXIST || stat(prefix, &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
                prefix[i] = saved;
                free(prefix);
                return MIOA_APP_DATA_IO;
            }
        }
        prefix[i] = saved;
    }
    free(prefix);
    return MIOA_APP_DATA_OK;
}
#endif

static mioa_app_data_status_v1 resolve_path(const char *app_name, char **out) {
#ifdef _WIN32
    char *base = NULL;
    mioa_app_data_status_v1 status = local_app_data(&base);
    if (status != MIOA_APP_DATA_OK) return status;
    char *path = join_path(base, app_name, '\\');
    free(base);
    if (!path) return MIOA_APP_DATA_NO_MEMORY;
    *out = path;
    return MIOA_APP_DATA_OK;
#else
    const char *base = NULL;
    char *owned = NULL;
    char separator = '/';
#if defined(__APPLE__)
    base = getenv("HOME");
    if (!base || base[0] != '/') return MIOA_APP_DATA_UNAVAILABLE;
    owned = join_path(base, "Library", separator);
    if (!owned) return MIOA_APP_DATA_NO_MEMORY;
    char *next = join_path(owned, "Application Support", separator);
    free(owned);
    if (!next) return MIOA_APP_DATA_NO_MEMORY;
    owned = join_path(next, app_name, separator);
    free(next);
#else
    base = getenv("XDG_DATA_HOME");
    if (base && base[0] == '/') {
        owned = join_path(base, app_name, separator);
    } else {
        base = getenv("HOME");
        if (!base || base[0] != '/') return MIOA_APP_DATA_UNAVAILABLE;
        owned = join_path(base, ".local", separator);
        if (!owned) return MIOA_APP_DATA_NO_MEMORY;
        char *next = join_path(owned, "share", separator);
        free(owned);
        if (!next) return MIOA_APP_DATA_NO_MEMORY;
        owned = join_path(next, app_name, separator);
        free(next);
    }
#endif
    if (!owned) return MIOA_APP_DATA_NO_MEMORY;
    *out = owned;
    return MIOA_APP_DATA_OK;
#endif
}

mioa_app_data_status_v1 mioa_app_data_directory_v1(const char *app_name,
    uint32_t options, mioa_app_data_path_v1 *out) {
    if (!out || out->path || out->length || !valid_app_name(app_name) ||
        (options & ~MIOA_APP_DATA_CREATE_DIRECTORY_V1))
        return MIOA_APP_DATA_INVALID;
    char *path = NULL;
    mioa_app_data_status_v1 status = resolve_path(app_name, &path);
    if (status != MIOA_APP_DATA_OK) return status;
    if (options & MIOA_APP_DATA_CREATE_DIRECTORY_V1) {
        status = ensure_directory(path);
        if (status != MIOA_APP_DATA_OK) { free(path); return status; }
    }
    out->path = path;
    out->length = strlen(path);
    return MIOA_APP_DATA_OK;
}

void mioa_app_data_path_release_v1(mioa_app_data_path_v1 *path) {
    if (!path) return;
    free(path->path);
    path->path = NULL;
    path->length = 0;
}
