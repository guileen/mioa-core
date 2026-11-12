# Quality validation

## Intent
完善测试框架，而非扩大 Zig 替换范围。复用社区资产，证明契约违反会被发现；结果可复现、可审计，不以数量、覆盖率或全绿代替正确性。

## Delivery
一个可重复的 Entities/PNG 闭环：固定 WHATWG/html5lib/PNG 社区输入、真实原生适配器与 libpng、Mioa/Chromium/Firefox 差分、至少十个有效契约变异、确定性 Fuzz→保留失败签名的 ddmin→确认后回归，以及 G0–G4 机器报告。

公开 Core `tests/` 归属适配器、语料与回归；私有产品 `quality/` 归属编排、浏览器差分与产品门禁。复用现有 native/web/PNG runner。第三方资产记录精确 revision、校验值、许可与选择范围；不自造 WPT runner。

## Boundaries
实体 lookup 与 HTML tokenizer/attribute context 分开；RGBA 编码与 Canvas/WebGL 语义分开。先验收当前闭环，不声称全 WPT、全 PNG 解码、内存安全或产品发布通过。不修改消费者 pin/Profile，不升级 Zig。缺工具或适配器是 NOT_RUN，不伪装 UNSUPPORTED。

## Reused assets and roadmap
P0: WHATWG、html5lib-tests、W3C PNG/libpng、Chromium/Firefox；WPT 使用官方 runner 与固定 revision，未有 Mioa 产品适配的范围明确 NOT_RUN。P1: AFL++/Domato/Atheris 根据目标接口和工具链兼容性接入；本闭环先提供确定性独立 targets、seed 与缩减/回归接口。Test262 只用于独立 JS 引擎验证。自动 mutation 工具须先确认 Zig 兼容性；本次使用定向源变异，不为工具升级编译器。

## Acceptance
见 [spec](specs/quality-validation/spec.md)、[design](design.md)、[tasks](tasks.md)。实现、自检、独立评审与用户接受分别记账，未运行项不勾选。
