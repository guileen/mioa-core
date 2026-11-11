# Rust 处理进度（只追加，一行一条，不改历史）

规则：语言不是指标；能力、帧级等价与延迟才是。任何“已去 Rust”的表述必须能指出可出帧的替代实现。

- 2026-11-11 “src/rust 已移除”仅指 `runtime/engine/src/rust/`（`ffi`、`html5ever`、`render`）；该树 `engine/build.zig` 已无 `cargo`/`linkRust`。
- 2026-11-11 `runtime/renderer/`（obscura）仍是 Rust，`runtime/build.mjs:284-286` 仍调 `cargo build -p obscura-cli --features render`；仓库不是 Rust-free。
- 2026-11-11 C++ 替身目前是空壳：`render_bridge.cpp` 的 `lp_render_png` 返回 0 字节、`html5ever_parse_document` 为空函数 ⇒ 无法出帧。
- 2026-11-11 pin `915cc3c` 仍含 `engine/src/rust` 三 crate，`engine/build.zig:380-382` 链接 `obscura-render`/`obscura-dom`/`obscura-ssrf`；desktop bundle 按 `core-pin.json` 校验，所以性能测量只能跑 pin 血缘。
- 2026-11-11 依赖噪音：`zig-pkg/…/bssl-*` 下有 94 个 `.rs`（BoringSSL 上游 Rust 绑定），不参与编译，勿计入“残留”。
- 2026-11-11 决定：双轨——obscura 作为出货后端维护，C++ bridge 作为替换候选；两轨共用“脏区 + 分块摘要”渲染设计。
- 2026-11-11 成本基线（obscura）：`paint_us ≈ 0.44ms + 面积 × 7.8ns/px`；整幅 2560×1524 约 30.9ms，达标 120Hz 必须走脏矩形。
- 2026-11-11 口径：性能与兼容结论一律标注后端（`obscura` / `cpp-bridge`），跨后端不可直接比较。
- 2026-11-11 12:17 定调：**清退 obscura 是终局目标**——实测每像素 7.8ns/px（整幅 2560×1524 ≈30.9ms），比填充下界高 5–15×，任何“整幅重绘”路径都到不了 120Hz。
- 2026-11-11 12:17 过渡手段：**fork/改造 obscura**（脏区光栅 + 128px 分块摘要 + slot 语义）；实测脏帧 1689µs，且与全幅重绘逐像素一致（`MIOA_PAINT_VERIFY` 10 × ok）。
- 2026-11-11 12:17 清退判据（三项缺一不可）：① C++ 渲染器在同一 7 行基准上 `cpu95 ≤8.33ms`；② 与 obscura 截图逐像素等价；③ 7 行门禁全绿。在此之前 obscura 是唯一可测后端。
- 2026-11-11 12:17 现状：C++ 桥仍是空壳（`lp_render_png` 返 0 字节、`html5ever_parse_document` 空函数），因此“改造 obscura”不是可选优化，而是过渡期唯一能拿到 120Hz 基线的路径；其脏区/分块设计直接作为 C++ 渲染器的规格输入。
