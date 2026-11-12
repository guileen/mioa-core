#!/usr/bin/env python3
"""Static Core portability boundary. Not a sandbox or a complete dependency audit."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOTS = ('mioa-core/mioa_core', 'runtime/engine/src')
EXTENSIONS = {'.py', '.zig', '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.m', '.mm', '.rs'}
IGNORED = {'.git', '.local', '.venv', '__pycache__', '.zig-cache', 'zig-out', 'zig-pkg', 'target', 'node_modules'}
PY_MODULES = {
    'os', 'pathlib', 'socket', '_socket', 'subprocess', 'ctypes', 'cffi', 'fcntl',
    'mmap', 'signal', 'select', 'selectors', 'resource', 'platform', 'multiprocessing',
    'threading', 'time', 'secrets', 'tempfile', 'shutil', 'glob', 'pty', 'termios',
    'tty', 'winreg', 'msvcrt', 'Quartz', 'AppKit', 'Foundation', 'Metal', 'objc',
    'urllib.request', 'http.client', 'http.server', 'webbrowser',
}
PY_CALLS = {
    'open', 'builtins.open', 'io.open', 'io.FileIO', '_io.open', '_io.FileIO',
    'asyncio.open_connection', 'asyncio.open_unix_connection', 'asyncio.start_server',
    'asyncio.start_unix_server', 'asyncio.create_subprocess_exec', 'asyncio.create_subprocess_shell',
    'asyncio.run', 'asyncio.new_event_loop', 'asyncio.to_thread',
    'sys.platform', 'sys.getwindowsversion', 'sys.stdin', 'sys.stdout', 'sys.stderr',
    'sys.__stdin__', 'sys.__stdout__', 'sys.__stderr__',
    'datetime.datetime.now', 'datetime.datetime.utcnow', 'datetime.date.today',
}
PRIVATE = {'mioa-graphics', 'mioa-browser', 'mioa_graphics', 'graphics_host',
           'graphics_renderer', 'graphics_scene_renderer_bridge', 'graphics_render',
           'mioa-os', 'mioa_os'}
NATIVE_MODULES = {'posix', 'libc', 'windows', 'windows_sys', 'winapi', 'nix', 'objc', 'metal', 'cocoa', 'AppKit', 'Foundation', 'UIKit', 'Quartz', 'CoreFoundation', 'CoreGraphics', 'IOSurface', 'Metal', 'mach', 'SDL', 'SDL2', 'SDL3', 'sdl', 'glfw', 'vulkan', 'OpenGL'}
HEADERS = {'unistd.h', 'fcntl.h', 'pthread.h', 'poll.h', 'signal.h', 'dirent.h',
           'dlfcn.h', 'time.h', 'syslog.h', 'windows.h', 'winsock2.h', 'sandbox.h',
           'filesystem', 'thread', 'mutex', 'shared_mutex', 'condition_variable', 'chrono', 'random'}
OS_FUNCTIONS = {'open', 'openat', 'fopen', 'freopen', 'close', 'read', 'write', 'pread', 'pwrite',
                'stat', 'lstat', 'fstat', 'mkdir', 'unlink', 'rename', 'opendir', 'readdir',
                'mmap', 'munmap', 'mprotect', 'malloc', 'calloc', 'realloc', 'free',
                'socket', 'socketpair', 'connect', 'bind', 'listen', 'accept', 'send', 'recv',
                'sendmsg', 'recvmsg', 'poll', 'select', 'fork', 'execve', 'execv', 'posix_spawn',
                'waitpid', 'wait4', 'kill', 'getpid', 'getuid', 'getenv', 'setenv',
                'clock_gettime', 'gettimeofday', 'nanosleep', 'getrandom', 'getentropy',
                'dlopen', 'dlsym', 'pthread_create', 'CreateFile', 'CreateProcess', 'VirtualAlloc'}
STDLIB_PATHS = {'c', 'posix', 'os', 'fs', 'net', 'process', 'Thread', 'thread', 'time'}


def private(name):
    return any(part in PRIVATE for part in re.split(r'[/\\.:]+', name))


def blocked_module(name):
    return any(name == item or name.startswith(item + '.') for item in PY_MODULES) or private(name)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + '.' + node.attr if base else ''
    return ''


def python_findings(source):
    tree = ast.parse(source)
    aliases = {}
    rows = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                if blocked_module(item.name):
                    rows.add((node.lineno, 'python-import', item.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            for item in node.names:
                name = module + '.' + item.name if module else item.name
                aliases[item.asname or item.name] = name
                pure_path = module == 'pathlib' and item.name in {'PurePath', 'PurePosixPath', 'PureWindowsPath'}
                if not pure_path and (blocked_module(module) or blocked_module(name)):
                    rows.add((node.lineno, 'python-import', name))
    for node in ast.walk(tree):
        name = dotted(node.func if isinstance(node, ast.Call) else node)
        head, dot, rest = name.partition('.')
        name = aliases.get(head, head) + (dot + rest if dot else '')
        if name in PY_CALLS:
            rows.add((node.lineno, 'python-os-api', name))
        if isinstance(node, ast.Call) and name in {'__import__', 'builtins.__import__', 'importlib.import_module'}:
            value = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in {'name', 'module'}), None)
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                rows.add((node.lineno, 'dynamic-import', 'nonliteral module'))
            elif blocked_module(value.value):
                rows.add((node.lineno, 'python-import', value.value))
    return rows


# Comments and literal contents are not executable references. Keep literal
# tokens solely for import/include inspection; never eval target source.
LEX = re.compile(r'(?P<comment>//[^\n]*|/\*.*?\*/)|(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])*\'|\\\\[^\n]*)|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<punct>::|->|[^\s])', re.S)


def native_findings(source, suffix):
    tokens = []
    line, end = 1, 0
    for match in LEX.finditer(source):
        line += source[end:match.start()].count('\n')
        if match.lastgroup != 'comment':
            tokens.append((match.group(), line, match.lastgroup == 'string'))
        line += match.group().count('\n')
        end = match.end()
    rows = set()
    std_aliases = {'std'}
    for i, (value, lineno, literal) in enumerate(tokens):
        if literal:
            before = [t[0] for t in tokens[max(0, i - 3):i]]
            is_import = before[-2:] in [['import', '('], ['cInclude', '('], ['include', '(']] or (before and before[-1] in {'include', 'import', 'require'})
            if not is_import:
                continue
            module = value[1:-1]
            parts = re.split(r'[/\\]+', module)
            system_header = before[-2:] == ['cInclude', '('] or (before and before[-1] in {'include', 'import'})
            if private(module) or any(p in NATIVE_MODULES for p in parts) or module in HEADERS or (system_header and module.startswith(('sys/', 'mach/', 'servers/', 'bsm/'))):
                rows.add((lineno, 'native-import', module))
            if module == 'std' and i >= 5 and tokens[i-3][0] == '@' and tokens[i-4][0] == '=':
                std_aliases.add(tokens[i-5][0])
            if module in {'std', 'builtin'} and i+3 < len(tokens) and tokens[i+2][0] == '.':
                member = tokens[i+3][0]
                if (module == 'std' and member in STDLIB_PATHS) or (module == 'builtin' and member == 'os'):
                    rows.add((lineno, 'native-os-api', module + '.' + member))
            continue
        if value in {'include', 'import'} and i and tokens[i-1][0] == '#' and i+1 < len(tokens) and tokens[i+1][0] == '<':
            names = []
            for text, _, _ in tokens[i+2:]:
                if text == '>':
                    break
                names.append(text)
            header = ''.join(names)
            if header in HEADERS or header.startswith(('sys/', 'mach/', 'servers/', 'bsm/')) or any(p in NATIVE_MODULES for p in header.split('/')) or private(header):
                rows.add((lineno, 'native-import', header))
        if value in NATIVE_MODULES and i+1 < len(tokens) and tokens[i+1][0] in {'::', '.'}:
            rows.add((lineno, 'native-os-api', value))
        if value in std_aliases and i+2 < len(tokens) and tokens[i+1][0] in {'.', '::'}:
            member = tokens[i+2][0]
            tail = [t[0] for t in tokens[i+2:i+5]]
            pure_units = member == 'time' and len(tail) == 3 and tail[1] in {'.', '::'} and (tail[2].startswith(('ns_per_', 'us_per_', 'ms_per_', 's_per_')) or tail[2] == 'epoch')
            pure_path = member == 'fs' and len(tail) >= 3 and tail[1] in {'.', '::'} and tail[2] == 'path'
            pure_os_raw = member == 'os' and len(tail) >= 3 and tail[1] in {'.', '::'} and tail[2] == 'raw'
            pure_c_env = member == 'c' and len(tail) >= 3 and tail[1] in {'.', '::'} and tail[2] in {'getenv', 'environ'}
            if (member in STDLIB_PATHS and not (pure_units or pure_path or pure_os_raw or pure_c_env)) or tail in [['heap', '.', 'page_allocator'], ['heap', '.', 'c_allocator'], ['Io', '.', 'Threaded']]:
                rows.add((lineno, 'native-os-api', value + tokens[i+1][0] + ''.join(tail)))
            if suffix == '.rs' and member == '{':
                for j in range(i+3, len(tokens)):
                    if tokens[j][0] == ';':
                        break
                    if tokens[j][0] in STDLIB_PATHS and tokens[j-1][0] in {'{', ','}:
                        rows.add((tokens[j][1], 'native-os-api', value + '::' + tokens[j][0]))
        if value in {'__APPLE__', '__linux__', '_WIN32', '_WIN64', 'TARGET_OS_MAC', 'TARGET_OS_IPHONE'}:
            rows.add((lineno, 'platform-conditional', value))
        if value in OS_FUNCTIONS and i+1 < len(tokens) and tokens[i+1][0] == '(' and (not i or tokens[i-1][0] not in {'.', '->'}):
            rows.add((lineno, 'native-os-api', value))
        if re.match(r'^(?:IOSurface|CF[A-Z]|CG[A-Z]|MTL[A-Z]|mach_|bootstrap_|pthread_)', value):
            rows.add((lineno, 'native-os-api', value))
    return rows


# Ordered: specific platform operations precede broad module families.
CAPABILITIES = (
    ('surface', r'IOSurface|CoreGraphics|CoreFoundation|^CF[A-Z]|^CG[A-Z]|^MTL[A-Z]|Metal', '隐藏平台图像类型；保留共享快路径，明确借用与完成。'),
    ('ipc', r'mach_|bootstrap_|socketpair|sendmsg|recvmsg|SCM_RIGHTS', '统一通道及句柄转移；明确认证、截断和失败释放。'),
    ('file-lock', r'flock|lockf|fcntl', '保留锁/描述符操作差异；区分租约、非阻塞和继承策略。'),
    ('durability', r'fsync|fdatasync|replace|rename', '区分原子可见与持久完成，覆盖文件及目录同步。'),
    ('temporary', r'tempfile|mkstemp|mkdtemp|tmpfile', '安全创建并绑定寿命，隐藏宿主临时路径。'),
    ('mapping', r'mmap|munmap|mprotect|VirtualAlloc|PageAllocator', '明确区域、权限、映射寿命及本地快路径。'),
    ('random', r'secrets|urandom|getrandom|getentropy|crypto\.random', '提供安全随机字节；令牌格式留在业务层。'),
    ('clock', r'^time\.|std(?:::|\.)time\.|datetime|clock_gettime|gettimeofday|nanosleep|setitimer', '明确单调/墙钟、精度、休眠和取消语义。'),
    ('events', r'selectors?|epoll|kqueue|kevent|eventfd|\bpoll\b|asyncio\.(run|new_event_loop)', '统一就绪/唤醒/取消；保留原生批处理后端。'),
    ('process', r'subprocess|multiprocessing|process\.(spawn|run|exit|abort|cleanExit)|fork|execve|execv|posix_spawn|waitpid|wait4|CreateProcess', '明确启动、能力继承、退出与终止，不暴露宿主进程表示。'),
    ('threads', r'threading|pthread_|std(?:::|\.)[Tt]hread|asyncio\.to_thread|^thread$', '统一执行与等待；本地任务不强制变成IPC。'),
    ('faults', r'signal|sigaction|sigwait|sigprocmask|getrlimit|setrlimit|core_dump', '区分生命周期通知与崩溃紧急出口，避免依赖普通锁/分配。'),
    ('environment', r'getenv|setenv|environ|process\.(Args|Environ|Init|executablePath)|getpid|getuid|geteuid', '注入明确启动上下文，避免业务随处读写宿主全局状态。'),
    ('terminal', r'termios|\btty\b|tcgetattr|tcsetattr|ioctl|STDIN|STDOUT|STDERR|sys\.(stdin|stdout|stderr)', '表达终端能力、模式及恢复，不迁走交互语义。'),
    ('network', r'socket|std(?:::|\.)net|urllib\.request|http\.(client|server)|\b(connect|bind|listen|accept|send|recv)\b|asyncio\.(open|start)', '统一连接/流/背压；平台调优通过显式能力保留。'),
    ('memory', r'allocator|malloc|calloc|realloc|\bfree\b', 'OS提供区域与额度，本地分配器保留；区分测试回调。'),
    ('file-metadata', r'stat|mkdir|unlink|remove|rmdir|chmod|chown|opendir|readdir|access|symlink|readlink', '统一目录权限与身份，不暴露inode/UID等宿主布局。'),
    ('files', r'pathlib|std(?:::|\.)fs|std\.Io\.Dir|shutil|glob|\b(open|openat|fopen|close|read|write|pread|pwrite|ftruncate)\b', '统一受限句柄与短读写；保留偏移/批量I/O优化。'),
    ('platform', r'platform|uname|builtin\.|__APPLE__|__linux__|_WIN|sys\.platform', '查询所需特征，不让业务分支依赖OS名称。'),
    ('foreign', r'ctypes|cffi|dlopen|dlsym', '隔离动态装载与FFI旁路，不能仅改名包装。'),
)
RULES = [(key, re.compile(pattern), tip) for key, pattern, tip in CAPABILITIES]
TIPS = {key: tip for key, _, tip in CAPABILITIES}
TIPS['review'] = '归属未解析；先判断宿主能力、纯类型/计算、业务同名或宿主工具，勿自动迁移。'


def category(reference):
    return next((key for key, pattern, _ in RULES if pattern.search(reference)), 'review')


def collect(source, suffix, gate_rows, lex, dotted, blocked=None):
    rows = set()
    def add(line, reference, confidence, kind):
        rows.add((line, reference, category(reference), confidence, kind))
    if suffix == '.py':
        tree = ast.parse(source)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                for item in node.names:
                    aliases[item.asname or item.name] = (node.module + '.' if node.module else '') + item.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = dotted(node.func)
            head, dot, tail = name.partition('.')
            resolved = aliases.get(head, head) + dot + tail
            imported_host = head in aliases and (blocked is None or blocked(aliases[head]) or aliases[head] == 'asyncio')
            if category(resolved) != 'review' and (imported_host or resolved in {'open', 'builtins.open'}):
                # Syntax-resolved module names, not a claim of scope/type resolution.
                add(node.lineno, resolved, 'candidate', 'call')
    else:
        tokens = []
        line, end = 1, 0
        for match in lex.finditer(source):
            line += source[end:match.start()].count('\n')
            if match.lastgroup != 'comment':
                tokens.append((match.group(), line, match.lastgroup))
            line += match.group().count('\n')
            end = match.end()
        def chain(i):
            names = [tokens[i][0]]
            j = i + 1
            while j + 1 < len(tokens) and tokens[j][0] in {'.', '::'} and tokens[j+1][2] == 'name':
                names.extend((tokens[j][0], tokens[j+1][0]))
                j += 2
            return ''.join(names), j
        aliases = {}
        for i in range(len(tokens) - 2):
            if tokens[i][0] == 'const' and tokens[i+1][2] == 'name' and tokens[i+2][0] == '=':
                j = i + 3
                if j + 3 < len(tokens) and [t[0] for t in tokens[j:j+3]] == ['@', 'import', '(']:
                    aliases[tokens[i+1][0]] = tokens[j+3][0].strip('"')
                elif j < len(tokens) and tokens[j][2] == 'name':
                    aliases[tokens[i+1][0]] = chain(j)[0]
        def resolve(name):
            visited = set()
            while True:
                head = re.split(r'\.|::', name)[0]
                if head not in aliases or head in visited:
                    return name
                visited.add(head)
                name = aliases[head] + name[len(head):]
        for i, (text, lineno, kind) in enumerate(tokens):
            if kind != 'name' or (i and tokens[i-1][0] in {'.', '::', '->'}):
                continue
            name, j = chain(i)
            if j >= len(tokens) or tokens[j][0] != '(':
                continue
            resolved = resolve(name)
            # Narrow inventory to known host namespaces; bare C names need review.
            qualified = bool(re.match(
                r'^std[.:]+(?:posix|c|fs|os|net|process|Thread|thread|time|'
                r'Io\.(?:Dir|File|net|Timestamp|Clock|Threaded)|heap\.(?:page_allocator|c_allocator))'
                r'(?:\.|::)', resolved)) or resolved.startswith(('posix.', 'libc.', 'c.', 'os.'))
            if resolved.startswith(('std.fs.path.', 'std::os::raw', 'std.c.getenv', 'std.c.environ', 'c.getenv', 'c.environ')):
                qualified = False
            elif resolved.startswith(('c.', 'std.c.', 'libc.')):
                leaf = resolved.rsplit('.', 1)[-1]
                qualified = bool(re.fullmatch(
                    r'open|openat|fopen|fclose|close|read|write|pread|pwrite|'
                    r'ftruncate|fsync|fdatasync|stat|lstat|fstat|mkdir|unlink|rename|'
                    r'mmap|munmap|mprotect|malloc|calloc|realloc|free|socket|socketpair|'
                    r'connect|bind|listen|accept|send|recv|sendmsg|recvmsg|poll|select|'
                    r'fork|execve|waitpid|wait4|kill|getpid|getuid|getenv|setenv|'
                    r'clock_gettime|gettimeofday|nanosleep|getrandom|getentropy|'
                    r'fcntl|dup2?|pipe2?|ioctl|sigaction|sigwait|raise|exit|_exit|'
                    r'kevent|kqueue|epoll_[a-z0-9_]+|eventfd', leaf))
            platform = bool(re.match(r'^(IOSurface|CF[A-Z]|CG[A-Z]|MTL[A-Z]|mach_|bootstrap_|pthread_)', resolved))
            if (qualified or platform) and category(resolved) != 'review':
                add(lineno, resolved, 'candidate', 'call')
    covered = {row[0] for row in rows}
    for line, rule, reference in gate_rows:
        if line in covered and rule.endswith('os-api'):
            continue
        # Broad guard references remain review, never silently waived.
        add(line, reference, 'review', 'import' if 'import' in rule else 'reference')
    return [dict(line=line, reference=reference, capability=capability,
                 confidence=confidence, kind=kind)
            for line, reference, capability, confidence, kind in sorted(rows)]


def summarize(occurrences):
    groups = {}
    for row in occurrences:
        key = row['capability']
        group = groups.setdefault(key, {'capability': key, 'candidate': 0, 'review': 0,
                                        'apis': {}, 'locations': {}, 'guidance': TIPS[key]})
        group[row['confidence']] += 1
        rank = 0 if row['confidence'] == 'candidate' else 1
        for field, value in [('apis', row['reference']), ('locations', f"{row['path']}:{row['line']}")]:
            group[field][value] = min(rank, group[field].get(value, rank))
    return [dict(group, apis=sorted(group['apis'], key=lambda value: (group['apis'][value], value)),
                 locations=sorted(group['locations'], key=lambda value: (group['locations'][value], value)))
            for _, group in sorted(groups.items())]


def render(report, capability=None, details=False):
    inventory = report['inventory']
    print(f"leak-direct-os-api: {report['status']}; {report['files_checked']} files; "
          f"{len(report['findings'])} gate refs; {len(inventory['groups'])} capability groups")
    print('Checklist候选≠已确认迁移；review需判归属。完整位置/API见JSON；--capability NAME --details展开。')
    for group in inventory['groups']:
        if capability and group['capability'] != capability:
            continue
        apis = group['apis'] if details else group['apis'][:3]
        locations = group['locations'] if details else group['locations'][:2]
        print(f"[ ] {group['capability']} candidate={group['candidate']} review={group['review']}: {group['guidance']}")
        print('  API: ' + ', '.join(apis) + (f" (+{len(group['apis'])-len(apis)})" if len(apis) < len(group['apis']) else ''))
        print('  at: ' + ', '.join(locations) + (f" (+{len(group['locations'])-len(locations)})" if len(locations) < len(group['locations']) else ''))
    for error in report['errors']:
        print(f"{error['path']}: ERROR: {error['error']}")


def scan(root):
    root = Path(root).resolve()
    findings, files, errors, occurrences = [], {}, [], []
    for relative in ROOTS:
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to(root):
            errors.append({'path': relative, 'error': 'required source root missing, symlinked or outside repository'})
            continue
        before = len(files)
        def walk_error(exc):
            errors.append({'path': relative, 'error': str(exc)})
        for current, dirs, names in os.walk(directory, followlinks=False, onerror=walk_error):
            for name in sorted(dirs):
                path = Path(current) / name
                if name not in IGNORED and path.is_symlink():
                    errors.append({'path': path.relative_to(root).as_posix(), 'error': 'symlinked source directory'})
            dirs[:] = sorted(name for name in dirs if name not in IGNORED and not (Path(current) / name).is_symlink())
            for name in sorted(names):
                path = Path(current) / name
                if path.suffix not in EXTENSIONS:
                    continue
                name = path.relative_to(root).as_posix()
                if path.is_symlink():
                    errors.append({'path': name, 'error': 'symlinked source entry'})
                    continue
                try:
                    data = path.read_bytes()
                    files[name] = hashlib.sha256(data).hexdigest()
                    source = data.decode('utf-8')
                    rows = python_findings(source) if path.suffix == '.py' else native_findings(source, path.suffix)
                    findings.extend({'path': name, 'line': line, 'rule': rule, 'reference': reference} for line, rule, reference in sorted(rows))
                    occurrences.extend(dict(row, path=name) for row in collect(source, path.suffix, rows, LEX, dotted, blocked_module))
                except (OSError, UnicodeError, SyntaxError) as exc:
                    errors.append({'path': name, 'error': str(exc)})
        if len(files) == before:
            errors.append({'path': relative, 'error': 'required source root contains no source files'})
    if not files:
        errors.append({'path': '.', 'error': 'no source files inspected'})
    return {'schema': 1, 'check': 'leak-direct-os-api', 'status': 'ERROR' if errors else 'FAIL' if findings else 'PASS',
            'scope': list(ROOTS), 'files_checked': len(files), 'sources': files,
            'findings': findings, 'errors': errors,
            'inventory': {'groups': summarize(occurrences), 'occurrences': occurrences,
                          'limitations': 'Advisory syntax-based candidates, not type/scope resolution. Review does not waive gate findings.'},
            'limitations': 'Static reference guard only; transitive library behavior, generated code and runtime syscall confinement require separate enforcement.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report', type=Path)
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    parser.add_argument('--capability', choices=tuple(TIPS))
    parser.add_argument('--details', action='store_true', help='expand API/location lists; optionally select --capability')
    args = parser.parse_args(argv)
    report = scan(args.root)
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.report:
        args.report.write_text(encoded)
    if args.format == 'json':
        print(encoded, end='')
    else:
        render(report, args.capability, args.details)
    return 0 if report['status'] == 'PASS' else 2 if report['status'] == 'ERROR' else 1


if __name__ == '__main__':
    raise SystemExit(main())
