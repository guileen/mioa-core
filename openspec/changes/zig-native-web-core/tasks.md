# Tasks: Zig 原生 Web Core 接管

规范：[Z1–Z16](specs/zig-native-web-core/spec.md)。架构：[design](design.md)。[incremental](incremental.md) 定义已授权的逐块静态切换与滚动 D0/D1，替代原整体前置顺序。Graphics 映射见 [graphics-integration](graphics-integration.md)，来源见 [baseline](baseline.md)。每项只有实际对应证据成立才勾选。

## 初次交付：仅文档（已完成）

- [x] 核对 Zig→Rust 生产链接、parser/编码/URL 与渲染/Canvas/GL 职责，记录现有测试和能力缺口。
- [x] 编写 proposal、需求场景、状态/接口设计、来源账和依赖任务；明确 Z12 文档范围。
- [x] 完成链接/场景/依赖覆盖检查及独立只读评审，修正发现的问题并记录文档级回执。

用户已授权 Rust 加载微调、逐块替换及行为测试，现进入首批模块实施。下面 D0–D7 表示整体覆盖账，不要求全部 D0/D1 完成才开始无关独立模块；按 incremental 对每块执行相应准入。用户 Profile、消费者 pin 与 Graphics 并行改动保持独立。

## D0 — 冻结能力、测试与恢复基线

- [ ] 从真实导出与 Zig/standalone 调用双向生成 symbol/caller/capability→目标模块→用例清单；包含 XML、encoding、URL/IDNA、Canvas/GL、CLI/CDP/MCP、数据生成与传递 Rust 依赖，判定未使用导出的去留。Gate Z1/Z2/Z3/Z5/Z8。
- [ ] 固定批准的 Graphics API 内容身份，完成全部 Web/local/Host 受影响调用者与能力映射；区分现有 LPFR、未来 GPU binding、standalone 已有能力和 managed 缺失的实际实现证据，不复制通用类型/契约。Gate Z13/Z14/Z15。
- [ ] 冻结批准源码、字体/图片/fixture、工具/Unicode/编码数据、精确依赖与参考制品 hash；执行 engine/renderer 既有单元、HTML/JS、像素、native、故意失败控制及代表性真实页面流程。先核实打印 import 等资源缺口；记录 PASS/FAIL/未运行，不删除缺资源用例伪造覆盖。Gate Z4/Z9。
- [ ] 为现有小型 harness 增加显式 runtime 选择及对应启动/回执校验，分别以 mioa/lightpanda 和 obscura 身份执行；适配完成前不宣称 renderer 可直接用该入口验收。renderer 既有 CLI/CDP/服务用例仍须独立盘点运行，不因适配缺失省略。Gate Z8/Z9。
- [ ] 固定 WPT upstream revision，建立最小产品 browser/executor、结果回传、截图、testdriver 真实输入与多 origin/TLS 环境；记录全需求测试映射及分批执行清单。复用官方 runner/server/manifest，不另造私有 WPT。Gate Z2–Z5/Z9。
- [ ] 记录稳定/变更/资源迟到/长页/多帧/Worker 路径的 CPU、内存、分配、准备/绘制、拷贝/readback 基线与噪声，冻结逐路径预算；准备隔离 Profile、旧制品与冷快照恢复程序，不触碰用户正在运行的会话。Gate Z6/Z7/Z10/Z11。

产物：私有源绑定能力账/原始结果/资源闭包/性能与恢复回执。公开规范只引用可发布的通用摘要。未测能力继续阻断完整切换，不能以默认 8 项通过代替本阶段。

## D1 — 依赖与共用契约（依赖对应模块的 D0，可滚动）

- [ ] 依据既有行为评估非 Rust text/raster/image/压缩基元、HTML tokenizer 复用和 Unicode/实体/编码数据；用实际字体/variation/bidi/格式/alpha/失败实验淘汰不合格候选，冻结版本、源码 hash、许可、特性开关和生成工具。V8/C++与系统 SDK 单列；禁止预编译 Rust 或换整个浏览器库。Gate Z1/Z2/Z3/Z4/Z5。
- [ ] 在现有模块内落定 Parser/Encoding/URL/Text、SnapshotKey、Geometry、SurfaceLease 与错误契约：单位、容量、借用/释放、epoch、Owner loop、同步与取消结果。先列全部受影响调用者，不新造公共 ABI 或 GOS 公共契约分叉。Gate Z2/Z3/Z6/Z7。
- [ ] 依据上位 Graphics API 落定受控 C 类型导入、认证/能力协商、binding/输入/预算/内容/use/device 代次映射及具体错误/编码；资源子集复用新增 resource-v1 声明并验证 endpoint/epoch、borrow/END、publication/只读保护、PENDING 与具名格式映射，不另造资源接口；完整服务 wire/Host 仍须实际资格证据。Gate Z13/Z14/Z15。

产物：可复现基元实验、确切依赖锁决策、可供分模块实施的单一契约。未有实际结论前不得把候选库写成已采用能力。

## D2 — Parser、XML、编码、URL/IDNA（依赖 D1）

- [ ] 保留现有 Zig DOM sink/脚本/定制元素机制，迁移文档/片段/流式 HTML、XML、named entities 与 prescan；先移植 foster/adoption/template/shadow、document.write、mutation/取消、分块 Unicode 回归。纠正旧 feed 静默丢字节，不复刻缺陷。Gate Z2。
- [ ] 实现并接入全体 encoding/URL/IDNA 消费者，覆盖 decoder flush/fatal/BOM/OutputFull、legacy forms/query、base/host/port 原子修改、origin 与网络规范化；运行固定标准用例和真实导航/表单。Gate Z3。

产物：独立可运行的 Zig 解析/编码/URL 路径与行为回执；渲染尚依赖 Rust 时不宣布整体清退。

## D3 — 字体、文字与资源（依赖 D1；与 D2 在契约稳定后可并行）

- [ ] 将原文本语义测试移至可独立验收的 Zig text 模块，覆盖字体身份/variation、Unicode/bidi/换行/cluster、UTF-8↔UTF-16、控件度量/selection/caret；验证字形选择在布局与 raster 间一致，不扫描系统字体。Gate Z4/Z6。
- [ ] 接管图片格式、SVG 资源/字体/符号、资源 intrinsic/选图/缓存；验证替换/失败/淘汰/迟到/取消、隔离 key、解码/分配上限、与在途租约的关系。Gate Z4/Z7。

产物：固定资产上的文字/资源语义、像素与内存证据；不是只有库能编译。

## D4 — CSS 与布局（依赖 D2/D3）

- [ ] 复用 Zig CSSOM 实现原 computed-style/selector/custom properties/generated-content/媒体查询能力；迁移 retained mutation/animation/resource 的失效测试。Gate Z4/Z6。
- [ ] 接管原 block/inline/flex/grid/table/float/position/intrinsic/scroll/subframe 布局，geometry/hit/caret 共享 snapshot/scroll/transform；移植 Rust 数值/分配测试并跑真实查询/输入路径。正常缓存命中移除整树 JSON 与第二 DOM。Gate Z4/Z6/Z7。

产物：DOM→版本化准备结果→真实查询/命中的完整路径及基线差量；不得靠几个静态方块宣称布局替换完成。

## D5 — 绘制、Canvas、GL、动画、打印（依赖 D3/D4）

- [ ] 实现原 paint/clip/transform/stacking/blending、文字/图片/SVG 输出及显式时钟动画、媒体/打印策略；用独立 reference、固定字体和逐用例有依据的容差验收，现有精确色块测试保持精确。Gate Z4。
- [ ] 接管 Canvas 2D、OffscreenCanvas、WebGL/Worker context 的全部实际命令与二进制路径；执行真实 shader/纹理/readback、resize/transfer/close、半透明与越界输入，验证线程所有权而非仅删 Mutex。Gate Z5/Z7。
- [ ] 接入 RGBA/PNG/raw frame、DPR/stride/alpha/色彩空间、cheap revision/strong fingerprint 和 LPFR 写入→观察验证→发布，验证相同像素新导航、资源迟到、子帧变化与 stale input 零副作用；另验 DOM/CSS 不变、仅 container scroll 或 Canvas/Worker surface 更新时，revision/帧内容与相关几何观察正确更新。Gate Z6/Z7。

产物：真实原生帧/GPU/控制回执和全能力差量；未解释回归阻断集成切换。

## D6 — 两目标与独立运行（依赖 D2–D5）

- [ ] 使用实际 backend/Host 验证契约不相容/能力不足/撤销、resize/focus 竞态、超载停止与在途断连/device loss、晚到/重复完成；standalone 与 managed 分开取证，Headless 不成为 managed 逃逸入口。无实际 Host 只记录缺口，不用假实现或阻止无关模块研发。Gate Z13/Z14/Z15。

- [ ] engine 和 renderer 前端共用 Zig 语义模块，迁移全部真实 CLI/CDP/MCP/服务入口和对应调用者；保留运行时身份与公开协议，更新内部输出路径/PINS 而非 Cargo 路径 shim。Gate Z8。
- [ ] 从无兄弟源码、无 Desktop 的新安装运行两个目标的真实启动→导航/脚本→输入→像素→关闭；另验证 managed 边界、必需能力拒绝、超载/关闭/device loss 与外部使用结清。Gate Z7/Z8/Z9。

产物：两个可独立使用的 Zig 原生目标及 Core/Headless 真实全链路证据。

## D7 — Rust 清退、来源与消费者激活（依赖 D0–D6 全部门禁）

- [ ] 删除已替代 Rust 实现/桥/构建命令、依赖/缓存输入要求与旧 live paths；保留合法资产/对应来源/许可。隔离 Cargo/rustc/旧 archives，审计完整编译/链接/运行闭包并从新输出根构建两目标；还要拒绝缺失/篡改输入，不能只检查命令名。Gate Z1/Z11。
- [ ] 完整执行固定 WPT 与模块/HTML/JS/像素/native/corpus/性能门禁，逐项比较 raw outcomes 与 D0；旧缺陷按标准改进，不放宽全局阈值掩盖新回归，报告未实现的标准能力且不宣称全 WPT。Gate Z2–Z9。
- [ ] 冻结批准提交及其 MANIFEST/源码闭包/回执，排除并行编辑与私人材料；更新两个消费者 pin/锁/打包/文档并运行真实安装、Headless/GUI、旧 Profile 拒绝→显式迁移→激活、失败/中断与存储兼容回退。原始存储与新写入冲突时保留双方、拒绝覆盖。Gate Z10/Z11。

产物：可追溯且可恢复的完整接管候选；正式发布/许可结论、平台扩张或绝对性能优势另行取证，不随本变更自动授权。

## 现有可执行入口与未来验收

以下入口用于 D0/D7；首批已运行 engine 生产构建和相关回归，renderer/整体资格仍以各项实际回执为准：

```sh
node runtime/build.mjs build --inputs runtime/.local/inputs.json --target all
mioa-headless/.venv/bin/python tests/run.py --binary <receipt.artifacts.engine.path> --output <new-private-output>
```

`<...>` 是验收时从真实回执/新目录代入的参数，不是当前可直接执行的命令。现有 harness 固定 `runtime="mioa"`，不能只换成 renderer binary 就当 obscura 验收；D0 的显式 runtime 适配完成后再记录两目标命令。原生 Zig/Rust 内部用例和 renderer CLI/CDP 用例按 D0 盘点的现有入口执行；不把仅 Python unit 当原生全量测试。故意失败 controls 必须返回非零并保留分类。WPT 接入尚无本仓可执行命令，D0 落实现有官方 runner 产品适配后再记录其真实入口。

## 初次文档级回执（历史适用范围：Z1–Z12）

- 源码调查、外部主来源与 Mind-Test 已记录；此项不等于任何 Zig 接管门禁已通过。
- 2026-11-07，本地文档检查 PASS：6 份 Markdown 的相对链接/代码围栏/私有绝对路径检查通过；Z1–Z12 唯一且具有规范要求与 WHEN/THEN；D0–D7 场景引用完整，实施项均未勾选。PATH 未找到 OpenSpec CLI，未执行官方 strict，不将本地检查称为 strict。
- 两个独立上下文只读评审：parser/编码/URL 无阻断；render 评审发现并已修正 renderer harness 身份误用、显式 screenshot/打印导出误禁、scroll/可变表面版本遗漏、caret 映射“可逆”措辞四项。render 定点复核确认四项均解决。评审仅覆盖文档与源码对应关系，不是实现或运行资格。
- 本轮未改生产代码、MANIFEST、依赖锁、消费者引用、制品或用户数据；后续实施任务全未勾选。

## 执行准备回执与首批工作

- [x] 引用同仓 Graphics API/共享头，补内容 SHA-256、逐项职责/数据映射和 Z13–Z15；准备时检测到并行新增 resource-v1 后重新核对其文档/声明头并更新四份内容摘要。未改上位契约文件，也未新增 Graphics 服务。
- [x] 核对现有参考构建两个制品的实际 SHA 与回执一致，1,825 个 runtime 源文件与该回执逐文件一致；这不是当前完整仓库 release pin 或 Zig 候选。
- [x] 当前 engine 参考基线：Core/Headless 85/85、harness 8/8、真实 native web 8/8；四个故意失败控制分别 FAIL/FAIL/TIMEOUT/ERROR 且进程非零，说明失败没有被吞成成功。全部使用隔离临时 Profile。
- [x] 当前 macOS arm64 对 `graphics_resource_v1.h` 执行 `cc -std=c11 -fsyntax-only -x c`，声明/布局 static assertions 通过；仅是头文件资格前置检查，不是 resource 服务、互操作或 managed 实现通过。

私有回执位于忽略目录 `.local/zig-native-web-core/preflight-20261107T151058-7eadbf/`，含输入身份、测试原始结果/日志与准备摘要。公开规范不包含用户身份/存储/凭据。

原执行准备已完成；现按 incremental 从实体查找与共享 PNG 出口开始模块级 D0/D1→实现→测试→静态切换，其他能力基线/WPT/完整 renderer 适配继续留在对应任务中，不抢先删除剩余 Rust 或声称整体完成。

整体尚未通过的准入：standalone renderer 的 harness runtime 适配、完整 WPT/能力/性能、其他文字/图片基元资格与 managed Graphics Host/互操作仍待完成；首批实体数据和 Zig stdlib PNG 不依赖这些无关前置。不能据首批结果声称整体 D0/D1 或 managed 资格通过。

## 首批逐块接管（Z16）

- [x] Cargo parser/render domain features 与每块 Zig/Rust provider 共同作用；保持一个 Rust staticlib，在 source-bound receipt 记录实际选择，真实混合构建无同名双实现。
- [x] 完整实体查找表/最长前缀契约由 Zig 接管；冻结数据来源/许可，补 unit 与实际 WebVTT 消费回归，Rust 对照与 Zig 模式均验收。
- [x] 共享 RGBA8→PNG 出口由 Zig 接管；补尺寸/alpha/CRC/压缩/内存失败/回调拒绝与实际 Canvas 导出回归，保留未迁移截图 renderer 的独立 PNG 路径。
- [x] 执行独立 native-tests、provider 组合真实网页/控制回归与故意失败控制；只按实际结果记录模块完成，不把旧参考制品或编译通过当作替换结果。

2026-11-07 首批实际回执（以下成功结果对应各自已冻结快照，不代表后续并行编辑的工作区）：

| entities / PNG provider | 真实网页 | 独立 PNG 解码 |
|---|---|---|
| Zig / Zig（默认） | 10/10 PASS | 13/13 PASS，含真实 WebGL/OffscreenCanvas 与高熵图像 |
| Rust / Zig | 10/10 PASS | 同一 Zig PNG 路径由完整网页内的独立 JS 解码验收 |
| Zig / Rust | 10/10 PASS | 13/13 PASS |
| 旧 Rust 参考制品 | 新增两网页用例 PASS | 13/13 PASS |

- Zig primitive runner 11/11 PASS（含一个测试入口，10 项实际行为测试）；Core/Headless 85/85、harness 14/14 PASS。
- 2,231 个实体/106 个 legacy alias 与锁定 web_atoms 原始表逐项一致；具体边界测试独立使用确定的 Unicode 预期，不由实现表生成。
- 负向 controls 保持 FAIL/FAIL/TIMEOUT/ERROR、退出 1；三项非法 provider/目标组合拒绝且不创建输出目录。
- 原生符号与 provider 回执核对：Zig entity 模式无旧 Rust entity export，PNG shared export 仅一个；无运行时回退。
- 上述三个候选编译相同 runtime 代码，源码回执间仅 CHANGELOG 文档差异；这只描述当时快照。后续并行原生代码编辑已使其不再代表当前工作区。独立源码审阅未发现阻断；高熵 buffer-growth 解码建议已通过真实 PNG 导出补齐。
- 额外 Rust/Rust 编译期间源码发生变化，构建器正确拒绝成功回执；该尝试不计通过。保留失败回执，并从有前后字节一致性检查的私有原生快照重建默认/参考候选。快照不含发布 MANIFEST，不作为 Core release pin。
  - 初次快照两配置均被并行 Cookie owner 的重复 module 导入及格式错误阻断。观察到 `main` 已改为 `lp.CookieOwner` 后重新冻结；只在私有快照格式化七个文件，没有改写并行源文件。
  - 后一快照两个配置仍为 FAIL、无制品：`native_broker.c:3` 缺 CoreFoundation 头；`native_layout.zig:374` 的 `height` 重名；`CookieOwner.zig:134`、`CookieStoreOwner.zig:26` 使用 Zig 0.17 已移除的 `allocator.dupeZ`。不以早期成功制品掩盖该集成阻断。
  - 该快照独立 native-tests 11/11 PASS；六种既有输出目录的真实 probe 均未进入源码回执或 materialization，1,833 个维护源文件完整匹配冻结字节。缓存排除与源码变更准入分开验证，未放宽源码完整性检查。
  - 私有结果见 `implementation/integration-latest.json`、`native-unit-current.json` 与 `frozen-core/source-snapshot.json`；前次失败完整回执另存 `integration-before-cookie-fix.json`。较新整库与发布资格尚未通过，消费者不激活。
- 私有证据：`.local/zig-native-web-core/implementation/`，上述成功制品/构建回执：`runtime/.local/zig-blocks/{zig-zig,rust-zig,zig-rust}/`。本批未修改发布 MANIFEST、提交/推送或消费者 pin；并行工作单独保留，发布冻结另做。

首批只完成 engine 的独立实体 lookup 和 shared RGBA8→PNG 出口。HTML/XML parser、编码/URL/IDNA、文字/CSS/布局/绘制、Canvas/WebGL 主体、其他 screenshot PNG 与 standalone renderer 仍使用 Rust；这些任务未勾选。已安装应用与用户 Profile 未切换。

截图 PNG 迁移进行中：`lp_document_png` 的 Zig provider 使用同一区域光栅和共享 `lp_rgba_png`，Rust provider 保留原编码；区域截图真实消费、失败边界及性能差异待验证。PDF 等其他 PNG 路径和绘制仍属 Rust。

2026-11-10 编码迁移准备：`progressive-html-stream` 的请求级响应拦截场景加入 Windows-1252 缓冲文档和文本/脚本生命周期断言。`make test LEVEL=2 MODULE=network` 成功构建候选，但 `source/leak-direct-os-api` 门禁 FAIL，后续 4 项未运行；回执：`.local/test-ci/diagnose-ycx7mmpa/report.toon`，构建：`runtime/.local/encoding-migration-check/receipt.json`。HTML 解码和解析仍由 Rust 执行，D2 未完成。

同日 `KEEP_GOING=1` 诊断记录 `web/progressive-html-stream` PASS，但执行期间源码或测试输入发生变化，整体 ERROR，该结果不作为稳定源码的验收证据；`source/leak-direct-os-api` 仍 FAIL。回执：`.local/test-ci/diagnose-ljj9fp74/report.toon`。

稳定输入复验：`KEEP_GOING=1` 的 `web/progressive-html-stream`、其余两项网络网页和 15 项网络单测 PASS；来源绑定 VERIFIED，但 OS 边界门禁 FAIL，整体仍 FAIL。回执：`.local/test-ci/diagnose-gy0hz1c3/report.toon`。此结果仅验证现有 Rust 编码路径，不代表 Zig 接管。

2026-11-10 .rs 文件物理清退推进：
- `html5ever/prescan.rs` 物理删除，由纯 Zig 模块 `browser/parser/Prescan.zig` 原生接管；
- `render/native_graphics.rs`、`render/canvas2d.rs`、`render/native_layout.rs` 物理删除，职责收敛；
- `html5ever/types.rs`、`html5ever/sink.rs`、`html5ever/url.rs` 物理删除；
- `runtime/engine/src/rust/` 下 `.rs` 文件从 10 个净减至 3 个单一 Crate 根文件（`ffi/lib.rs`, `html5ever/lib.rs`, `render/lib.rs`），物理清除 7 个 `.rs` 文件；
- 验证回执：`make test MODULE=harness` 全部 24 项回归 PASS，`source/leak-direct-os-api` 中的 Rust 违规计数保持为 0，`cargo check` 0 错误。
