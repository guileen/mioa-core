# Change: Zig 接管原生 Web Core

## Why

主引擎使用 Zig，但生产构建调用 Cargo，把第一方 HTML/XML/编码/URL 适配器、渲染桥与 Rust 第三方实现链接进同一静态库。仅把 FFI 外壳改成 Zig 不会消除 Rust 依赖，也不能形成统一的 DOM、布局、资源与帧生命周期。

用户目标：原生 Web Core 的第一方实现由 Zig 全面接管，保留现有真实网页能力。用户现已授权先微调 Rust 加载，再逐块替换并完善测试；以 [incremental](incremental.md) 的模块级静态选择和逐块验收推进，不再等待无关全局门禁才允许首块开工。

## What Changes

- 用 Zig 接管 HTML/XML、编码、WHATWG URL/IDNA、CSS/布局/文字、绘制与资源、Canvas/WebGL 的现有第一方语义和集成。
- 替换对应的 Rust 传递依赖，包括 html5ever/xml5ever、encoding_rs、url/idna、Taffy、cosmic-text/Parley、tiny-skia、image、resvg/usvg 等承担的职责；不保留隐藏 Rust 静态库、子进程或失败回退。
- 重用现有 Zig DOM、Web API、导航、脚本、网络与存储；取消原生热路径的整树 JSON 投影和第二套可变 DOM 权威。
- 按运行时、环境、网络/存储、文档/脚本、渲染、观察/控制、对外契约划分职责；模块目录、状态与接口由 [design.md](design.md) 定义。
- 复用 WPT 官方设施和已有 HTML/JS/像素用例，迁移有行为价值的 Rust 测试。标准预期、已知缺口和迁移回归分开记录。
- 保留 engine/renderer 两个现有目标及独立 Headless 能力，最终都由 Zig 原生模块支撑；同步固定版本消费者、构建输入、来源和许可记录。
- 以同仓 [GOS 契约](../../../mioa-os/api/README.md) 和 [图形共享值类型](../../../mioa-os/api/graphics/graphics_types.h) 为上位契约；[接入映射](graphics-integration.md) 定义 Core/Host/local backend 职责、内容身份与执行准入，不另造接口或假定完整服务已实现。

## Scope and Non-goals

“全面”指项目控制的原生 Web 实现与产品依赖闭包，不等于重写操作系统或所有第三方代码。V8/C++、现有 C 基础库和系统图形接口可继续使用；新增第三方基元必须逐项记录版本、源码、许可、语言及能力证据。不得将 DOM/CSS/布局整体委托给另一个 C/C++ 浏览器来冒充 Zig 接管。

Python 控制/Profile API、Node 构建驱动不在语言重写范围；需要的调用适配在最终集成中同步。Desktop Shell、GOS 实现与其公共契约不迁入 Web Core。当前平台资格仍为 macOS arm64/CGL；不附带跨平台、全 WPT、零拷贝或性能优越性承诺。

不降低既有 Web/CSS 能力、不清空用户数据、不绕过 Profile 制品校验、不自动发布。模块可独立静态切换，剩余 Rust 明确记录；最终 Rust 清退和消费者激活仍要求全部对应门禁，不做运行失败回退。

## Acceptance

完整接管需要同时满足 [spec](specs/zig-native-web-core/spec.md) 的 Z1–Z16：无 Rust 原生生产闭包；覆盖 parser/编码/URL、渲染和两目标入口；无既有能力回归；状态/帧/控制安全成立；Graphics 契约映射与对应能力资格明确；源绑定构建、真实 Headless/消费者升级和恢复通过。逐块通过不等于整个 Core 已无 Rust。

原始来源、测试缺口与待执行实验见 [baseline.md](baseline.md)；执行与证据清单见 [tasks.md](tasks.md)。

## Status

2026-11-07：首批 named-character-reference、shared RGBA8→PNG 与静态加载适配已实现并通过模块/真实网页/混合 provider 验收，默认构建两块选择 Zig。整体 Rust 清退、完整 WPT/Graphics/消费者资格未完成；用户 Profile 与消费者 pin 未切换。覆盖范围与私有回执见 tasks，不把首批结果当作整个 Core 完成。
