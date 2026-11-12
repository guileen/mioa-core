// Private native V8 boundary. Reservation-only VA is free; accessible pages
// are admitted before commit. NoAccess alone and Discard are NOT release.
#include "v8_cpp_abi.h"
#include <v8.h>
#include <v8-platform.h>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <new>
#include <utility>

extern "C" {
void* lp_runtime_alloc(size_t, size_t);
void lp_runtime_free(void*);
bool lp_runtime_reserve(size_t);
void lp_runtime_release(size_t);
bool lp_runtime_pause();
void lp_runtime_resume(bool);
}
namespace {
struct Range { uintptr_t start, end; };
class AdmittedPages final : public v8::PageAllocator {
    // Bounded metadata: even hostile fragmentation cannot allocate bookkeeping
    // outside admission. Exhaustion fails the operation before committing.
    static constexpr size_t capacity = 16384;
    Range ranges[capacity];
    size_t count = 0;
    std::mutex mutex;
    v8::PageAllocator* inner;
    struct Lock {
        std::mutex& mutex;
        explicit Lock(std::mutex& m) : mutex(m) {
            if (mutex.try_lock()) return;
            bool suspended = lp_runtime_pause();
            mutex.lock();
            lp_runtime_resume(suspended);
        }
        ~Lock() { mutex.unlock(); }
    };
    static bool accessible(Permission p) {
        return p != kNoAccess && p != kNoAccessWillJitLater;
    }
    size_t covered(uintptr_t start, uintptr_t end) const {
        size_t bytes = 0;
        for (size_t i = 0; i < count; ++i) {
            auto a = std::max(start, ranges[i].start);
            auto b = std::min(end, ranges[i].end);
            if (a < b) bytes += b - a;
        }
        return bytes;
    }
    bool canRemove(uintptr_t start, uintptr_t end) const {
        if (count < capacity) return true;
        for (size_t i = 0; i < count; ++i) {
            if (ranges[i].start < start && ranges[i].end > end) return false;
        }
        return true;
    }
    void add(uintptr_t start, uintptr_t end) {
        size_t first = 0;
        while (first < count && ranges[first].end < start) ++first;
        size_t last = first;
        while (last < count && ranges[last].start <= end) {
            start = std::min(start, ranges[last].start);
            end = std::max(end, ranges[last].end);
            ++last;
        }
        std::memmove(ranges + first + 1, ranges + last, (count - last) * sizeof(Range));
        count = count - (last - first) + 1;
        ranges[first] = {start, end};
    }
    void remove(uintptr_t start, uintptr_t end) {
        size_t bytes = covered(start, end);
        for (size_t i = 0; i < count;) {
            auto r = ranges[i];
            if (r.end <= start || r.start >= end) { ++i; continue; }
            if (r.start < start && r.end > end) {
                std::memmove(ranges + i + 2, ranges + i + 1, (count - i - 1) * sizeof(Range));
                ranges[i] = {r.start, start};
                ranges[i + 1] = {end, r.end};
                ++count;
                break;
            }
            if (r.start < start) { ranges[i++].end = start; continue; }
            if (r.end > end) { ranges[i++].start = end; continue; }
            std::memmove(ranges + i, ranges + i + 1, (count - i - 1) * sizeof(Range));
            --count;
        }
        lp_runtime_release(bytes);
    }
    template<class Action> bool commit(void* address, size_t length, Permission p, Action action) {
        Lock lock(mutex);
        auto start = reinterpret_cast<uintptr_t>(address);
        if (length > UINTPTR_MAX - start || count == capacity) return false;
        size_t bytes = accessible(p) ? length - covered(start, start + length) : 0;
        if (!lp_runtime_reserve(bytes)) return false;
        if (!action()) { lp_runtime_release(bytes); return false; }
        if (accessible(p) && length) add(start, start + length);
        return true;
    }
public:
    explicit AdmittedPages(v8::PageAllocator* allocator) : inner(allocator) {}
    size_t AllocatePageSize() override { return inner->AllocatePageSize(); }
    size_t CommitPageSize() override { return inner->CommitPageSize(); }
    void SetRandomMmapSeed(int64_t seed) override { inner->SetRandomMmapSeed(seed); }
    void* GetRandomMmapAddr() override { return inner->GetRandomMmapAddr(); }
    void* AllocatePages(void* address, size_t length, size_t alignment, Permission p) override {
        Lock lock(mutex);
        if (count == capacity) return nullptr;
        const bool charged = accessible(p);
        const size_t page = inner->AllocatePageSize();
        const size_t slack = charged && alignment > page ? alignment - page : 0;
        if (slack > SIZE_MAX - length) return nullptr;
        const size_t bytes = charged ? length + slack : 0;
        if (!lp_runtime_reserve(bytes)) return nullptr;
        void* result = inner->AllocatePages(address, length, alignment, p);
        if (!result) { lp_runtime_release(bytes); return nullptr; }
        if (slack) lp_runtime_release(slack);
        if (charged && length) add(reinterpret_cast<uintptr_t>(result), reinterpret_cast<uintptr_t>(result) + length);
        return result;
    }
    bool FreePages(void* address, size_t length) override {
        Lock lock(mutex);
        auto start = reinterpret_cast<uintptr_t>(address);
        if (!canRemove(start, start + length) || !inner->FreePages(address, length)) return false;
        remove(start, start + length);
        return true;
    }
    bool ReleasePages(void* address, size_t length, size_t remaining) override {
        Lock lock(mutex);
        auto start = reinterpret_cast<uintptr_t>(address);
        if (remaining > length || !canRemove(start + remaining, start + length) ||
            !inner->ReleasePages(address, length, remaining)) return false;
        remove(start + remaining, start + length);
        return true;
    }
    bool SetPermissions(void* address, size_t length, Permission p) override {
        return commit(address, length, p, [&] { return inner->SetPermissions(address, length, p); });
    }
    bool RecommitPages(void* address, size_t length, Permission p) override {
        return commit(address, length, p, [&] { return inner->RecommitPages(address, length, p); });
    }
    bool DiscardSystemPages(void* address, size_t length) override {
        // The OS may leave these physically backed, and writes can restore them
        // without a commit callback. Never make this credit locally reusable.
        return inner->DiscardSystemPages(address, length);
    }
    bool DecommitPages(void* address, size_t length) override {
        Lock lock(mutex);
        auto start = reinterpret_cast<uintptr_t>(address);
        if (!canRemove(start, start + length) || !inner->DecommitPages(address, length)) return false;
        remove(start, start + length);
        return true;
    }
    bool SealPages(void* address, size_t length) override { return inner->SealPages(address, length); }
    // Shared-page remapping defaults to unsupported; it must not bypass credit.
};

// Keep the existing OffsetClockPlatform as the public handle: its C bridge
// downcasts for foreground notifications, time offsets and message-loop calls.
// Only V8's installed platform is this forwarding facade.
class AdmittedPlatform final : public v8::Platform {
    v8::Platform* inner;
public:
    AdmittedPages pages;
    explicit AdmittedPlatform(v8::Platform* p) : inner(p), pages(p->GetPageAllocator()) {}
    v8::PageAllocator* GetPageAllocator() override { return &pages; }
    v8::ThreadIsolatedAllocator* GetThreadIsolatedAllocator() override { return inner->GetThreadIsolatedAllocator(); }
    void OnCriticalMemoryPressure() override { inner->OnCriticalMemoryPressure(); }
    int NumberOfWorkerThreads() override { return inner->NumberOfWorkerThreads(); }
    std::shared_ptr<v8::TaskRunner> GetForegroundTaskRunner(v8::Isolate* i, v8::TaskPriority p) override { return inner->GetForegroundTaskRunner(i, p); }
    bool IdleTasksEnabled(v8::Isolate* i) override { return inner->IdleTasksEnabled(i); }
    std::unique_ptr<v8::ScopedBoostablePriority> CreateBoostablePriorityScope() override { return inner->CreateBoostablePriorityScope(); }
    std::unique_ptr<v8::ScopedBlockingCall> CreateBlockingScope(v8::BlockingType b) override { return inner->CreateBlockingScope(b); }
    double MonotonicallyIncreasingTime() override { return inner->MonotonicallyIncreasingTime(); }
    double CurrentClockTimeMillis() override { return inner->CurrentClockTimeMillis(); }
    int64_t CurrentClockTimeMilliseconds() override { return inner->CurrentClockTimeMilliseconds(); }
    double CurrentClockTimeMillisecondsHighResolution() override { return inner->CurrentClockTimeMillisecondsHighResolution(); }
    StackTracePrinter GetStackTracePrinter() override { return inner->GetStackTracePrinter(); }
    v8::TracingController* GetTracingController() override { return inner->GetTracingController(); }
    void DumpWithoutCrashing() override { inner->DumpWithoutCrashing(); }
    v8::HighAllocationThroughputObserver* GetHighAllocationThroughputObserver() override { return inner->GetHighAllocationThroughputObserver(); }
protected:
    std::unique_ptr<v8::JobHandle> CreateJobImpl(v8::TaskPriority p, std::unique_ptr<v8::JobTask> t, const v8::SourceLocation& l) override { return inner->CreateJob(p, std::move(t), l); }
    void PostTaskOnWorkerThreadImpl(v8::TaskPriority p, std::unique_ptr<v8::Task> t, const v8::SourceLocation& l) override { inner->PostTaskOnWorkerThread(p, std::move(t), l); }
    void PostDelayedTaskOnWorkerThreadImpl(v8::TaskPriority p, std::unique_ptr<v8::Task> t, double delay, const v8::SourceLocation& l) override { inner->PostDelayedTaskOnWorkerThread(p, std::move(t), delay, l); }
};
AdmittedPlatform* installed = nullptr;
class AdmittedBuffers final : public v8::ArrayBuffer::Allocator {
    size_t maximum;
public:
    explicit AdmittedBuffers(size_t max) : maximum(max) {}
    void* Allocate(size_t length) override {
        auto result = AllocateUninitialized(length);
        if (result) std::memset(result, 0, length);
        return result;
    }
    void* AllocateUninitialized(size_t length) override { return length <= maximum ? lp_runtime_alloc(length, 16) : nullptr; }
    void Free(void* memory, size_t) override { lp_runtime_free(memory); }
    size_t MaxAllocationSize() const override { return maximum; }
    v8::PageAllocator* GetPageAllocator() override { return installed ? &installed->pages : nullptr; }
};
}
extern "C" {
bool lp_runtime_v8_install(v8::Platform* original) {
    if (installed || !original->GetPageAllocator()) return false;
    auto backing = lp_runtime_alloc(sizeof(AdmittedPlatform), alignof(AdmittedPlatform));
    if (!backing) return false;
    installed = new(backing) AdmittedPlatform(original);
    v8::V8::InitializePlatform(installed);
    return true;
}
void lp_runtime_v8_dispose() {
    if (!installed) return;
    installed->~AdmittedPlatform();
    lp_runtime_free(installed);
    installed = nullptr;
}
size_t lp_runtime_heap_ceiling();
// The first isolate fixes the shared range; macOS precommits it before discard.
void lp_runtime_v8_configure_code_range(v8::Isolate::CreateParams* params) {
    const size_t limit = lp_runtime_heap_ceiling() / 2;
    const size_t configured = params->constraints.code_range_size_in_bytes();
    if (!configured || configured > limit) params->constraints.set_code_range_size_in_bytes(limit);
}
v8::ArrayBuffer::Allocator* lp_runtime_v8_buffers(size_t maximum) {
    auto backing = lp_runtime_alloc(sizeof(AdmittedBuffers), alignof(AdmittedBuffers));
    return backing ? new(backing) AdmittedBuffers(std::min(maximum, lp_runtime_heap_ceiling())) : nullptr;
}
void lp_runtime_v8_buffers_delete(v8::ArrayBuffer::Allocator* value) {
    static_cast<AdmittedBuffers*>(value)->~AdmittedBuffers();
    lp_runtime_free(value);
}
}
