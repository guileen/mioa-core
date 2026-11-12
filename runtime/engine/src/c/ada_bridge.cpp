#include <cstdlib>
#include <cstring>
#include <string>
#include <cstdio>
extern "C" {
#include "ada_c.h"
}

struct OwnedString {
    uint8_t* ptr;
    size_t len;
};

extern "C" {

int32_t lpurl_domain_to_ascii(
    const uint8_t* host_ptr,
    size_t host_len,
    uint8_t** out_ptr,
    size_t* out_len
) {
    if (!host_ptr || !out_ptr || !out_len) return 1;
    ada_owned_string res = ada_idna_to_ascii((const char*)host_ptr, host_len);
    if (!res.data) return 1;
    uint8_t* buf = (uint8_t*)malloc(res.length + 1);
    if (!buf) {
        ada_free_owned_string(res);
        return 1;
    }
    memcpy(buf, res.data, res.length);
    buf[res.length] = 0;
    *out_ptr = buf;
    *out_len = res.length;
    ada_free_owned_string(res);
    return 0;
}

void lpurl_free(uint8_t* ptr, size_t len) {
    (void)len;
    if (ptr) free(ptr);
}

void free_owned_string(OwnedString owned) {
    if (owned.ptr) free(owned.ptr);
}

ada_url url_parse(const uint8_t* ptr, size_t len, int32_t* err) {
    if (!ptr) { if (err) *err = 1; return nullptr; }
    ada_url u = ada_parse((const char*)ptr, len);
    if (!ada_is_valid(u)) {
        ada_free(u);
        if (err) *err = 1;
        return nullptr;
    }
    if (err) *err = 0;
    return u;
}

ada_url url_parse_with_base(const uint8_t* base_ptr, size_t base_len, const uint8_t* ptr, size_t len, int32_t* err) {
    if (!ptr || !base_ptr) { if (err) *err = 1; return nullptr; }
    ada_url u = ada_parse_with_base((const char*)ptr, len, (const char*)base_ptr, base_len);
    if (!ada_is_valid(u)) {
        ada_free(u);
        if (err) *err = 1;
        return nullptr;
    }
    if (err) *err = 0;
    return u;
}

ada_url url_join(const ada_url base, const uint8_t* ptr, size_t len, int32_t* err) {
    if (!base || !ptr) { if (err) *err = 1; return nullptr; }
    ada_string base_href = ada_get_href(base);
    ada_url u = ada_parse_with_base((const char*)ptr, len, base_href.data, base_href.length);
    if (!ada_is_valid(u)) {
        ada_free(u);
        if (err) *err = 1;
        return nullptr;
    }
    if (err) *err = 0;
    return u;
}

void url_free(ada_url url) {
    if (url) ada_free(url);
}

bool url_can_parse(const uint8_t* ptr, size_t len) {
    if (!ptr) return false;
    return ada_can_parse((const char*)ptr, len);
}

bool url_can_parse_with_base(const uint8_t* base_ptr, size_t base_len, const uint8_t* ptr, size_t len) {
    if (!ptr || !base_ptr) return false;
    return ada_can_parse_with_base((const char*)ptr, len, (const char*)base_ptr, base_len);
}

void url_to_string(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { *out_ptr = nullptr; *out_len = 0; return; }
    ada_string s = ada_get_href(url);
    *out_ptr = (const uint8_t*)s.data;
    *out_len = s.length;
}

void url_get_scheme(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { *out_ptr = nullptr; *out_len = 0; return; }
    ada_string p = ada_get_protocol(url);
    *out_ptr = (const uint8_t*)p.data;
    if (p.length > 0 && p.data[p.length - 1] == ':') {
        *out_len = p.length - 1;
    } else {
        *out_len = p.length;
    }
}

int32_t url_set_scheme(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_protocol(url, (const char*)ptr, len) ? 0 : -1;
}

void url_get_username(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { *out_ptr = nullptr; *out_len = 0; return; }
    ada_string u = ada_get_username(url);
    *out_ptr = (const uint8_t*)u.data;
    *out_len = u.length;
}

int32_t url_set_username(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_username(url, (const char*)ptr, len) ? 0 : -1;
}

int32_t url_get_password(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url || !ada_has_non_empty_password(url)) {
        if (out_len) *out_len = 0;
        return -1;
    }
    ada_string p = ada_get_password(url);
    *out_ptr = (const uint8_t*)p.data;
    *out_len = p.length;
    return 0;
}

int32_t url_set_password(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_password(url, (const char*)ptr, len) ? 0 : -1;
}

int32_t url_get_port(const ada_url url) {
    if (!url) return -1;
    ada_string p = ada_get_port(url);
    if (p.length == 0) return -1;
    int port = 0;
    for (size_t i = 0; i < p.length; i++) {
        if (p.data[i] < '0' || p.data[i] > '9') return -1;
        port = port * 10 + (p.data[i] - '0');
    }
    return port;
}

int32_t url_set_port(ada_url url, uint16_t port) {
    if (!url) return -1;
    char buf[16];
    int len = snprintf(buf, sizeof(buf), "%u", port);
    return ada_set_port(url, buf, len) ? 0 : -1;
}

int32_t url_set_port_to_null(ada_url url) {
    if (!url) return -1;
    ada_clear_port(url);
    return 0;
}

int32_t url_get_hostname(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url || !ada_has_hostname(url)) {
        if (out_len) *out_len = 0;
        return -1;
    }
    ada_string h = ada_get_hostname(url);
    *out_ptr = (const uint8_t*)h.data;
    *out_len = h.length;
    return 0;
}

int32_t url_set_hostname(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_hostname(url, (const char*)ptr, len) ? 0 : -1;
}

int32_t url_get_host(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url || !ada_has_hostname(url)) {
        if (out_len) *out_len = 0;
        return -1;
    }
    ada_string h = ada_get_host(url);
    *out_ptr = (const uint8_t*)h.data;
    *out_len = h.length;
    return 0;
}

int32_t url_set_host(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_host(url, (const char*)ptr, len) ? 0 : -1;
}

void url_get_path(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { *out_ptr = nullptr; *out_len = 0; return; }
    ada_string p = ada_get_pathname(url);
    *out_ptr = (const uint8_t*)p.data;
    *out_len = p.length;
}

int32_t url_set_path(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    return ada_set_pathname(url, (const char*)ptr, len) ? 0 : -1;
}

int32_t url_get_query(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { if (out_len) *out_len = 0; return -1; }
    ada_string s = ada_get_search(url);
    if (s.length == 0 || s.data[0] != '?') {
        if (out_len) *out_len = 0;
        return -1;
    }
    *out_ptr = (const uint8_t*)s.data + 1;
    *out_len = s.length - 1;
    return 0;
}

int32_t url_set_query(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    ada_set_search(url, (const char*)ptr, len);
    return 0;
}

void url_set_query_to_null(ada_url url) {
    if (url) ada_clear_search(url);
}

int32_t url_get_fragment(const ada_url url, const uint8_t** out_ptr, size_t* out_len) {
    if (!url) { if (out_len) *out_len = 0; return -1; }
    ada_string h = ada_get_hash(url);
    if (h.length == 0 || h.data[0] != '#') {
        if (out_len) *out_len = 0;
        return -1;
    }
    *out_ptr = (const uint8_t*)h.data + 1;
    *out_len = h.length - 1;
    return 0;
}

int32_t url_set_fragment(ada_url url, const uint8_t* ptr, size_t len) {
    if (!url || !ptr) return -1;
    ada_set_hash(url, (const char*)ptr, len);
    return 0;
}

void url_set_fragment_to_null(ada_url url) {
    if (url) ada_clear_hash(url);
}

OwnedString url_get_origin(const ada_url url) {
    if (!url) return { nullptr, 0 };
    ada_owned_string o = ada_get_origin(url);
    uint8_t* buf = (uint8_t*)malloc(o.length);
    if (buf && o.length > 0) {
        memcpy(buf, o.data, o.length);
    }
    OwnedString res = { buf, o.length };
    ada_free_owned_string(o);
    return res;
}

OwnedString url_resolve_without_encoding(
    const uint8_t* base_ptr, size_t base_len,
    const uint8_t* input_ptr, size_t input_len,
    int32_t* err
) {
    if (!input_ptr) {
        if (err) *err = -1;
        return { nullptr, 0 };
    }
    ada_url u = nullptr;
    if (base_ptr && base_len > 0) {
        u = ada_parse_with_base((const char*)input_ptr, input_len, (const char*)base_ptr, base_len);
    } else {
        u = ada_parse((const char*)input_ptr, input_len);
    }
    if (!ada_is_valid(u)) {
        if (u) ada_free(u);
        if (err) *err = -1;
        return { nullptr, 0 };
    }
    ada_string href = ada_get_href(u);
    uint8_t* buf = (uint8_t*)malloc(href.length);
    if (buf && href.length > 0) {
        memcpy(buf, href.data, href.length);
    }
    ada_free(u);
    if (err) *err = 0;
    return { buf, href.length };
}

OwnedString url_resolve_with_encoding(
    const uint8_t* base_ptr, size_t base_len,
    const uint8_t* input_ptr, size_t input_len,
    const uint8_t* enc_ptr, size_t enc_len,
    int32_t* err
) {
    (void)enc_ptr; (void)enc_len;
    return url_resolve_without_encoding(base_ptr, base_len, input_ptr, input_len, err);
}

} // extern "C"
