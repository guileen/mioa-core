#ifndef MIOA_V8_CPP_ABI_H
#define MIOA_V8_CPP_ABI_H
#include <__config>
#if !defined(_LIBCPP_ABI_VERSION) || _LIBCPP_ABI_VERSION != 2 || !defined(_LIBCPP_ABI_ENABLE_UNIQUE_PTR_TRIVIAL_ABI) || !defined(_LIBCPP_ABI_ENABLE_SHARED_PTR_TRIVIAL_ABI)
#error "V8 adapters require the pinned Chromium libc++ ABI v2 headers and config"
#endif
#if !defined(V8_COMPRESS_POINTERS) || defined(V8_ENABLE_SANDBOX)
#error "V8 adapters require the pinned pointer-compression and disabled-sandbox build"
#endif
#endif
