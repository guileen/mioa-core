// Private structured-clone boundary. All wrapper and serializer-buffer backing
// is admitted by the caller's allocator before allocation; V8 owns no fallback.
#include "v8_cpp_abi.h"
#include <v8.h>
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <new>

namespace {
struct CloneBool { bool has_value; bool value; };
struct CloneCallbacks {
    void* data;
    void (*throw_error)(void*, const v8::String*);
    CloneBool (*write_host)(void*, v8::Isolate*, const v8::Object*);
    bool (*shared_id)(void*, v8::Isolate*, const v8::SharedArrayBuffer*, uint32_t*);
};
using Allocate = void* (*)(void*, size_t);
using Dispose = void (*)(void*, void*, size_t);
template<class T> static v8::Local<T> local(const T* value) {
    static_assert(sizeof(v8::Local<T>) == sizeof(value));
    return *reinterpret_cast<const v8::Local<T>*>(&value);
}
class Delegate final : public v8::ValueSerializer::Delegate {
public:
    CloneCallbacks callbacks;
    void* memory;
    Allocate allocate;
    Dispose dispose;
    size_t capacity = 0;
    Delegate(CloneCallbacks c, void* m, Allocate a, Dispose d)
        : callbacks(c), memory(m), allocate(a), dispose(d) {}
    void ThrowDataCloneError(v8::Local<v8::String> message) override {
        if (callbacks.throw_error) callbacks.throw_error(callbacks.data, *message);
    }
    v8::Maybe<bool> WriteHostObject(v8::Isolate* isolate, v8::Local<v8::Object> object) override {
        if (callbacks.write_host) {
            auto result = callbacks.write_host(callbacks.data, isolate, *object);
            if (result.has_value) return v8::Just(result.value);
        }
        return v8::Nothing<bool>();
    }
    v8::Maybe<uint32_t> GetSharedArrayBufferId(v8::Isolate* isolate, v8::Local<v8::SharedArrayBuffer> buffer) override {
        uint32_t id = 0;
        if (callbacks.shared_id && callbacks.shared_id(callbacks.data, isolate, *buffer, &id)) return v8::Just(id);
        return v8::Nothing<uint32_t>();
    }
    void* ReallocateBufferMemory(void* old, size_t requested, size_t* actual) override {
        *actual = 0;
        void* next = allocate(memory, requested);
        if (!next) return nullptr; // The old allocation remains owned by V8.
        if (old) {
            std::memcpy(next, old, std::min(capacity, requested));
            dispose(memory, old, capacity);
        }
        capacity = requested;
        *actual = requested;
        return next;
    }
    void FreeBufferMemory(void* buffer) override {
        if (buffer) dispose(memory, buffer, capacity);
        capacity = 0;
    }
};
struct Serializer {
    Delegate delegate;
    v8::ValueSerializer value;
    Serializer(v8::Isolate* isolate, CloneCallbacks c, void* m, Allocate a, Dispose d)
        : delegate(c, m, a, d), value(isolate, &delegate) {}
};
} // namespace
static_assert(alignof(Serializer) <= 16);
extern "C" {
Serializer* lp_accounted_clone_new(v8::Isolate* isolate, const CloneCallbacks* callbacks,
                                  void* memory, Allocate allocate, Dispose dispose) {
    if (!isolate || !callbacks || !allocate || !dispose) return nullptr;
    void* backing = allocate(memory, sizeof(Serializer));
    return backing ? new(backing) Serializer(isolate, *callbacks, memory, allocate, dispose) : nullptr;
}
void lp_accounted_clone_delete(Serializer* self) {
    auto memory = self->delegate.memory;
    auto dispose = self->delegate.dispose;
    self->~Serializer();
    dispose(memory, self, sizeof(Serializer));
}
void lp_accounted_clone_header(Serializer* self) { self->value.WriteHeader(); }
void lp_accounted_clone_write(Serializer* self, const v8::Context* context, const v8::Value* value, CloneBool* out) {
    auto result = self->value.WriteValue(local(context), local(value));
    out->has_value = !result.IsNothing();
    out->value = result.FromMaybe(false);
}
uint8_t* lp_accounted_clone_release(Serializer* self, size_t* size, size_t* capacity) {
    auto buffer = self->value.Release();
    *size = buffer.second;
    *capacity = self->delegate.capacity;
    self->delegate.capacity = 0;
    return buffer.first;
}
void lp_accounted_clone_u32(Serializer* self, uint32_t value) { self->value.WriteUint32(value); }
void lp_accounted_clone_u64(Serializer* self, uint64_t value) { self->value.WriteUint64(value); }
void lp_accounted_clone_bytes(Serializer* self, const void* bytes, size_t length) { self->value.WriteRawBytes(bytes, length); }
}
