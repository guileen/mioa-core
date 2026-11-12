#include "sandbox.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <dirent.h>
#include <errno.h>

static int ensure_dir_recursive(const char *dir) {
    char tmp[1024];
    size_t len = strlen(dir);
    if (len >= sizeof(tmp)) return -1;
    memcpy(tmp, dir, len + 1);

    for (size_t i = 1; i < len; ++i) {
        if (tmp[i] == '/') {
            tmp[i] = '\0';
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
                return -1;
            }
            tmp[i] = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

static int resolve_bundle_executable(const char *app_path, char *out_exe, size_t max_len) {
    struct stat st;
    if (stat(app_path, &st) != 0) return -1;

    // Check if it's a directory (possible .app bundle)
    if (S_ISDIR(st.st_mode)) {
        char macos_dir[1024];
        snprintf(macos_dir, sizeof(macos_dir), "%s/Contents/MacOS", app_path);
        DIR *d = opendir(macos_dir);
        if (d) {
            struct dirent *ent;
            while ((ent = readdir(d)) != NULL) {
                if (ent->d_name[0] == '.') continue;
                snprintf(out_exe, max_len, "%s/%s", macos_dir, ent->d_name);
                closedir(d);
                return 0;
            }
            closedir(d);
        }
    }

    // Direct executable
    snprintf(out_exe, max_len, "%s", app_path);
    return 0;
}

static int extract_proxy_port(const mioa_sandbox_config_t *config) {
    if (config->proxy_port > 0) return config->proxy_port;
    const char *url = config->proxy_url ? config->proxy_url : config->http_proxy;
    if (url) {
        const char *colon = strrchr(url, ':');
        if (colon) {
            int p = atoi(colon + 1);
            if (p > 0 && p < 65536) return p;
        }
    }
    return 10801;
}
size_t mioa_sandbox_config_size(void) {
    return sizeof(mioa_sandbox_config_t);
}

int mioa_sandbox_launch(const mioa_sandbox_config_t *config, mioa_sandbox_process_t *out_proc) {
    if (!config || !config->app_path || !out_proc) return -1;
    if (config->proxy_port < 0 || config->proxy_port > 65535) {
        return -1; // ABI alignment error: proxy_port corrupt
    }
    memset(out_proc, 0, sizeof(*out_proc));

    char exe_path[1024];
    if (resolve_bundle_executable(config->app_path, exe_path, sizeof(exe_path)) != 0) {
        return -1;
    }

    // Fail-closed seatbelt check
    if (config->enforce_seatbelt) {
#ifdef __APPLE__
        const char *seatbelt_bin = "/usr/bin/sandbox-exec";
        if (access(seatbelt_bin, X_OK) != 0) {
            return -2; // Fail-closed: seatbelt requested but binary unavailable
        }
#else
        return -2; // Fail-closed: seatbelt not supported on this platform
#endif
    }

    const char *sandbox_dir = config->sandbox_dir ? config->sandbox_dir : "/tmp/mioa_sandbox";
    if (ensure_dir_recursive(sandbox_dir) != 0) return -3;

    char default_log[1024];
    const char *log_path = config->log_path;
    if (!log_path) {
        snprintf(default_log, sizeof(default_log), "%s/output.log", sandbox_dir);
        log_path = default_log;
    }

    // Prepare posix_spawn file actions for stdout/stderr redirection
    posix_spawn_file_actions_t actions;
    if (posix_spawn_file_actions_init(&actions) != 0) return -3;

    // Open log file for stdout and stderr
    int open_flags = O_WRONLY | O_CREAT | O_TRUNC;
    if (posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, log_path, open_flags, 0644) != 0 ||
        posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO) != 0) {
        posix_spawn_file_actions_destroy(&actions);
        return -3;
    }

    posix_spawnattr_t attr;
    if (posix_spawnattr_init(&attr) != 0) {
        posix_spawn_file_actions_destroy(&actions);
        return -3;
    }

    int port = extract_proxy_port(config);

    // Build isolated environment array
    char tz_buf[256];
    char lang_buf[256];
    char lc_buf[256];
    char proxy_buf[256];
    char http_proxy_buf[256];
    char home_buf[512];
    char tmp_buf[512];
    char app_lang_buf[256];
    char app_locale_buf[256];

    snprintf(tz_buf, sizeof(tz_buf), "TZ=%s", config->timezone ? config->timezone : "America/Los_Angeles");
    snprintf(lang_buf, sizeof(lang_buf), "LANG=%s", config->language ? config->language : "en_US.UTF-8");
    snprintf(lc_buf, sizeof(lc_buf), "LC_ALL=%s", config->language ? config->language : "en_US.UTF-8");
    snprintf(proxy_buf, sizeof(proxy_buf), "ALL_PROXY=%s", config->proxy_url ? config->proxy_url : "socks5://127.0.0.1:10801");
    snprintf(http_proxy_buf, sizeof(http_proxy_buf), "HTTP_PROXY=%s", config->http_proxy ? config->http_proxy : "http://127.0.0.1:10801");
    snprintf(home_buf, sizeof(home_buf), "HOME=%s", sandbox_dir);
    snprintf(tmp_buf, sizeof(tmp_buf), "TMPDIR=%s", sandbox_dir);
    snprintf(app_lang_buf, sizeof(app_lang_buf), "AppleLanguages=(%s)", config->apple_locale ? config->apple_locale : "en-US");
    snprintf(app_locale_buf, sizeof(app_locale_buf), "AppleLocale=%s", config->apple_locale ? config->apple_locale : "en_US");

    const char *envp[] = {
        "MIOA_ISOLATED=1",
        "MIOA_ENVIRONMENT_ID=us-west-isolated",
        tz_buf,
        lang_buf,
        lc_buf,
        proxy_buf,
        http_proxy_buf,
        "HTTPS_PROXY=http://127.0.0.1:10801",
        "NO_PROXY=localhost,127.0.0.1",
        home_buf,
        tmp_buf,
        app_lang_buf,
        app_locale_buf,
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        NULL
    };

    const char *spawn_bin = exe_path;
    char *const default_argv[] = { exe_path, NULL };
    char *const *argv_to_spawn = default_argv;

#ifdef __APPLE__
    const char *seatbelt_bin = "/usr/bin/sandbox-exec";
    char seatbelt_profile[1024];
    snprintf(seatbelt_profile, sizeof(seatbelt_profile),
        "(version 1)\n"
        "(allow default)\n"
        "(deny network-outbound (remote ip \"*:*\"))\n"
        "(allow network-outbound (remote ip \"localhost:%d\"))\n",
        port);

    char *const seatbelt_argv[] = {
        (char *)seatbelt_bin,
        (char *)"-p",
        (char *)seatbelt_profile,
        exe_path,
        NULL
    };

    if (config->enforce_seatbelt) {
        spawn_bin = seatbelt_bin;
        argv_to_spawn = seatbelt_argv;
    }
#endif

    pid_t pid = 0;
    int ret = posix_spawn(&pid, spawn_bin, &actions, &attr, argv_to_spawn, (char *const *)envp);

    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attr);

    if (ret != 0) {
        return -4;
    }

    out_proc->pid = (int)pid;
    out_proc->exited = false;
    out_proc->exit_code = 0;
    return 0;
}

int mioa_sandbox_poll(mioa_sandbox_process_t *proc, bool wait_for_exit) {
    if (!proc || proc->pid <= 0) return -1;
    if (proc->exited) return 0;

    int status = 0;
    int options = wait_for_exit ? 0 : WNOHANG;
    pid_t res = waitpid((pid_t)proc->pid, &status, options);

    if (res == (pid_t)proc->pid) {
        proc->exited = true;
        if (WIFEXITED(status)) {
            proc->exit_code = WEXITSTATUS(status);
        } else if (WIFSIGNALED(status)) {
            proc->exit_code = 128 + WTERMSIG(status);
        }
        return 0;
    } else if (res == 0) {
        return 1; // Still running
    }
    return -1;
}

int mioa_sandbox_read_log(const char *log_path, char *buf, size_t buf_size) {
    if (!log_path || !buf || buf_size == 0) return -1;
    int fd = open(log_path, O_RDONLY);
    if (fd < 0) return -1;

    ssize_t n = read(fd, buf, buf_size - 1);
    close(fd);

    if (n < 0) return -1;
    buf[n] = '\0';
    return (int)n;
}
