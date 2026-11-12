# 逐块静态接管

2026-11-07：用户明确授权调整 Rust 加载并逐块实现、完善测试。替代此前“全局基线全部完成才允许首块实施”的执行顺序；最终无 Rust 的范围不缩减，Graphics 上位契约不变。

## 加载规则

- 仍只有一个 `lightpanda_ffi` Rust staticlib。parser/render 为可显式选择的 Cargo domain features；不能拆出多个各带 Rust std/allocator 的 staticlib。
- 每个已落地的细分块使用编译期 provider（rust/zig），生产构建回执记录实际选择。没有运行时选择、失败回退、双派发或热切换。
- Zig provider 对应禁用同名 Rust export/实现，Rust provider 对应不导出冲突 Zig symbol。一个入口只有一个实现。未迁移模块继续使用 Rust，不属于兜底。
- 只有有完整替代的块才暴露 provider 选项。不得允许 `zig layout` 等未实现配置构建出空功能。
- 保留 Rust 对照路径用于有界迁移测试；最终该块/整体契约稳定后清理不再需要的对照代码与选项。旧模式不是长期兼容 API。

## 每块验收

1. 固定该块所有 caller、输入/输出/错误/生命周期、独立预期与既有能力基线。
2. 先加回归：合法行为、关键边界、异常/内存/资源失败，以及真实消费者路径；源码文字、符号存在、mock 转发不算行为证明。
3. 实现 Zig，在独立模块测试与所选真实引擎上验证。共享全局安全/网页回归持续运行；HTML、布局、文字等高耦合块另做组合验收。
4. 该块完成后静态切换，其余块保持当前实现。失败就不激活该候选，不在运行中悄悄调回 Rust。
5. 将模块状态、制品/源/配置、测试与缺口记入 tasks。只声明已验收的块，最终无 Rust 声明仍要求原 Z1–Z15 全部相关门禁。

D0/D1 改为全局安全基线加模块级准入滚动推进；URL/实体/PNG 等独立模块不等待整个 WPT、Graphics Host 或所有文本/布局依赖选型。涉及 managed binding/GPU 共享的模块仍须对应真实实现和资格，不能绕过。

## 首批有界块

| 块 | 完整替代边界 | 仍未替代的职责 |
|---|---|---|
| named character references | Zig 消费者用到的完整实体表查找、prefix/exact/no-match 与 WebVTT 消费 | HTML tokenizer/tree-builder 及其内部实体算法仍属 Rust parser |
| RGBA8 → PNG | `lp_rgba_png` 共享编码入口、Zig Canvas 与 Rust Canvas 调用该入口、回调/错误与实际导出 | 页面截图 renderer 的其他 PNG 编码、绘制/图片解码仍属 Rust |

这两个块是先实施的模块，不代表将整体接管目标降成两个辅助函数。HTML/XML/编码/URL、Canvas/GL、文字、CSS/布局/绘制继续按各自契约滚动实施。模块事实与当前阻断以 tasks 回执为准，未通过的模块不得标完成。
