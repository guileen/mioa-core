#ifndef MIOA_OS_SANDBOX_H
#define MIOA_OS_SANDBOX_H

#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *app_path;       // Path to target.app or executable
    const char *proxy_url;      // e.g. "socks5://127.0.0.1:10801"
    const char *http_proxy;     // e.g. "http://127.0.0.1:10801"
    int proxy_port;             // e.g. 10801 (narrowed seatbelt port rule)
    const char *timezone;       // e.g. "America/Los_Angeles"
    const char *language;       // e.g. "en_US.UTF-8"
    const char *apple_locale;   // e.g. "en_US"
    const char *sandbox_dir;    // Output/isolation directory
    const char *log_path;       // Path to write stdout/stderr log
    bool enforce_seatbelt;      // Enable macOS Seatbelt kernel network/file jail (fail-closed)
} mioa_sandbox_config_t;

typedef struct {
    int pid;
    int exit_code;
    bool exited;
    char log_preview[1024];
} mioa_sandbox_process_t;

// Launch an application inside the isolated sandbox (returns 0 on success, <0 on failure)
// Error codes:
// -1: Invalid arguments or executable resolution failed
// -2: Seatbelt requested but unavailable (fail-closed)
// -3: Directory creation or file action failed
// -4: posix_spawn failed
int mioa_sandbox_launch(const mioa_sandbox_config_t *config, mioa_sandbox_process_t *out_proc);

// Return sizeof(mioa_sandbox_config_t) for caller ABI verification
size_t mioa_sandbox_config_size(void);

// Poll or wait for sandbox process status
int mioa_sandbox_poll(mioa_sandbox_process_t *proc, bool wait_for_exit);

// Read sandbox log file
int mioa_sandbox_read_log(const char *log_path, char *buf, size_t buf_size);

#ifdef __cplusplus
}
#endif

#endif // MIOA_OS_SANDBOX_H
