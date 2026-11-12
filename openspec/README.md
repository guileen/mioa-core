# Core changes

本目录记录 Core 自身的目标契约、设计与验收，不包含私有 Host/Shell 实现或用户运行资料。

## 质量验证框架

[quality-validation](changes/quality-validation/proposal.md)：固定社区语料、独立 oracle、定向变异、差分、Fuzz/缩减/确认回归与 G0–G4 证据门禁；先验收 Entities/PNG 小闭环，不外推整库或发布资格。

- [需求与场景](changes/quality-validation/specs/quality-validation/spec.md)
- [所有权、报告、门禁与 Mind-Test](changes/quality-validation/design.md)
- [实施与实际验收账](changes/quality-validation/tasks.md)

## Zig 原生接管

[zig-native-web-core](changes/zig-native-web-core/proposal.md)：以 Zig 接管当前 Rust 原生生产职责，保留现有网页能力。首批实体 lookup/共享 PNG 和静态加载适配已实现，三种 provider 组合通过真实网页回归；整体 Rust 清退未完成。

- [需求与 Z1–Z16 场景](changes/zig-native-web-core/specs/zig-native-web-core/spec.md)
- [目录、所有权、接口与 Mind-Test](changes/zig-native-web-core/design.md)
- [逐块静态接管与每块验收](changes/zig-native-web-core/incremental.md)
- [同仓 Graphics API 规范引用与接入映射](changes/zig-native-web-core/graphics-integration.md)
- [原始来源与覆盖缺口](changes/zig-native-web-core/baseline.md)
- [依赖任务与验收记录](changes/zig-native-web-core/tasks.md)

现有可执行能力仍以根 README、runtime/README 与 tests/README 为准。各模块以实际新制品/行为证据验收，整体与消费者激活单独取证；不自动改动用户 Profile 或消费者 pin。
