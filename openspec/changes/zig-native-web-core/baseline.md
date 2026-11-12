# Baseline: 原始证据与迁移覆盖账

盘点日期：2026-11-07。以下为源码/测试存在性观察，除特别注明外不是本轮执行通过记录。实施前 D0 必须冻结精确源码、资产、工具/数据版本和逐用例结果；此文不把可变工作树当 release pin。所有路径相对仓库根。

## 1. 原始素材与方法

以实际生产调用、FFI、构建/依赖锁、活 DOM 集成、原始 HTML/CSS/JS、Rust 语义测试、固定字体与资源为输入；不只读 README 或旧阶段总结。方法：导出与调用者双向盘点、行为分组、状态/生命周期推演、同条件差量、独立 reference、固定 WPT 与浏览器交叉诊断。巨型渲染文件的旧 phase 注释不作为能力上限。

构建事实：
- [engine/build.zig](../../../runtime/engine/build.zig) `361–402` 定义 linkRust：Cargo frozen 构建、单一 `liblightpanda_ffi.a` 链接。
- [Rust workspace](../../../runtime/engine/src/rust/Cargo.toml) `1–7` 与 [ffi manifest](../../../runtime/engine/src/rust/ffi/Cargo.toml) `7–14`：parser 与 render 同属一个静态库，不能删一半就宣布清除 Rust。
- [render manifest](../../../runtime/engine/src/rust/render/Cargo.toml) `11–26` 和 [obscura-render manifest](../../../runtime/renderer/crates/obscura-render/Cargo.toml) `14–44`：Rust 的职责包含第三方算法/数据，不只是桥接。
- [native_spec.py](../../../mioa-core/mioa_core/native_spec.py) `24–28,58–63` 固定 obscura/lightpanda/mioa 运行时与两个制品路径；迁移必须处理这些调用契约，不能只顾默认目标。
- [runtime/build.mjs](../../../runtime/build.mjs) 与 [runtime/README.md](../../../runtime/README.md) 定义两构建目标、输入校验和回执；不能直接移除 Cargo 参数却遗留 Rusty V8、Cargo cache、源码闭包与制品路径约束。

## 2. Parser / 编码 / URL 生产闭包

下表路径前缀 `E = runtime/engine/src/`。导出族是盘点分组；D0 还须生成精确 symbol→caller→replacement→test 清单，查明未被当前 caller 使用的导出是删除还是其他目标仍需保留。

| 职责 | 当前 Rust 入口 / 原始证据 | Zig 消费者与必须覆盖的行为 |
|---|---|---|
| HTML document / fragment | `rust/html5ever/lib.rs:34–158,482–615`，`html5ever_parse_document{,_with_encoding}` / `html5ever_parse_fragment` | `browser/parser/Parser.zig:232–287,322–359`：上下文 namespace、template、SVG/MathML、foster parenting、adoption agency、定制元素反应 |
| 流式与 sink | 同文件 `647–776`；`types.rs:23–70`、`sink.rs:92–379` | `Parser.zig:361–476`：feed/finish/destroy、document.write 排队、节点重挂载、回调借用/失败、finish 消费句柄 |
| XML / 实体 / 预扫描 | `lib.rs:459–480,778–863,963–1110`；`prescan.rs:44–204` | DOMParser、fragment、WebVTT named entities、脚本预加载/去重、XML 有效性；不能用 HTML parser 代替 XML |
| 编码 | `lib.rs:177–457`：label、decode、stateful decoder、legacy encode/NCR | TextDecoder、导航、CSS charset、FileReader、FormData/KeyValueList；BOM/fatal/flush、buffer consumed/written、旧编码输出 |
| WHATWG URL | `rust/html5ever/url.rs:133–840`：parse/base/join/canParse、组件 getters/setters、origin、编码感知 resolve | `sys/url.zig`、`browser/URL.zig`、Web URL/URLPattern；base 无效、opaque/file/special URL、端口/host 修改与 legacy query |
| IDNA | `url.rs:82–131` 的 `lpurl_domain_to_ascii` / `lpurl_free` | `sys/idna.zig`，网络在 curl 前规范化；UTS#46 非 transitional/非 strict，与单纯 punycode 或其他 IDNA 配置不同 |

真实风险：
- 现有流式 feed 对每个 chunk 独立做 UTF-8 验证，无效 chunk 会跳过并返回成功（`lib.rs:718–749`）。这是已发现实现缺陷，不应作为新版本兼容目标；须用跨 chunk 编码测试纠正。
- 字符串/attribute iterator 多为回调期间借用；URL getter 借用在 mutation/free 后失效，origin/resolve 的 OwnedString 与 IDNA 字符串有不同释放方式。新 Zig API 要消除混用，不把旧 ABI 地址当长期权威。
- XML validation 与 tree construction 分离；已有错误文本过滤应迁成明确分类，不能误把恢复错误或 operational failure 静默吞掉。
- 现有片段上下文与 quirks 传播存在限制，不能在规格中声称已有完整规范支持。标准用例是改进依据，不能只比较旧序列化输出。

可复用 Zig：现有 Parser/DOM 节点工厂、脚本/定制元素/observer 检查点、charset 选择、percent 编码工具；`sys/idna.zig` 和 `sys/url.zig` 当前仍是 Rust 包装，不是现成替代算法。

## 3. Render / Canvas / Graphics 生产闭包

路径缩写：`R = runtime/renderer/crates/obscura-render`，`F = runtime/renderer/render-repros`，`B = runtime/engine/src/rust/render`。

| 接管族 | 行为与原始证据 | 复用与补验 |
|---|---|---|
| CSS/布局 | `R/tests/layout_test.rs:268–528,1269–1808,2699–3014,3139–3466`：表格/float/flex/grid、单位、级联/变量、generated boxes | 保留数值断言；提取 inline HTML，复用 F 的 table/float/flex/grid/containing-block/CSSOM fixtures |
| 文字/字体 | `R/src/inline.rs:4415,5142–5203,5305–5337`；`R/tests/word_spacing_shaping.rs` | bidi、字距、换行、variation/opsz、字体身份、同源度量/raster/caret；保留固定字体及 `tests/fonts/obscura-vf-test.woff2.b64` |
| Paint/资源/SVG | `R/src/paint.rs:1047–1069,12600–12771,15052–15183,15821–15913,16480–16540,17336–17549` | prepared scene、clip/opacity/transform、选图/intrinsic、SVG 字体/currentColor/sprite；资源到达/替换/失败/淘汰分别验证 |
| Canvas 2D | `B/canvas2d.rs:170–218,337–389` | 路径、clip、合成、半透明、图片、像素、PNG、resize；复用 engine `browser/tests/canvas/canvas_rendering_context_2d.html` |
| WebGL/Worker surface | `B/native_graphics.rs:8–91` | GL Mutex/线程所有权、纹理子区间、readback、后端配置；复用 `native_webgl.html`、`offscreen_canvas.html`，不是 CPU 假像素替代 |
| 动画/媒体/打印 | `R/src/lib.rs:1967–2127`；`R/src/paint.rs:3028–3090,17124–17165,19101–19183` | 显式时钟/rewind、动态字体/图片、screen/print 状态恢复；已见媒体证据是 poster 与文本轨数据，不证明视频解码/播放/字幕绘制完整 |
| 命中/滚动/caret | `R/src/paint.rs:1573–1620,1736–1767,18420–18995`；`B/native_layout.rs:114–165,344–455` | stacking/clip/shared scroll、UTF-16 DOM 与 UTF-8 controls、子帧边框/scale 映射；rotation/skew 的现有明确拒绝不能伪造几何 |
| retained/cache/performance | `R/src/dom.rs:393–420,574–599`；`R/tests/flat_tree_allocations.rs`；`R/src/paint.rs:14235–14387,16095,19605` | 变更分类、资源取消/淘汰、offscreen decode、分配上界；这些是 Rust 内部语义测试，必须先移植，不能只保留 HTML 截图 |

关键跨边界事实：
- [native_layout.zig](../../../runtime/engine/src/browser/native_layout.zig) `53–148` 在 cache-hit 检查前遍历/投影/JSON/哈希；随后 Rust `B/native_layout.rs:46–98` 建另一套 DOM。取消这一热路径复制不等于取消派生布局树。
- 同一 Zig 文件 `169–185` 以借用回调提供 Canvas/iframe 像素；`354–432` 区分 cheap revision、像素摘要与递归 document/frame identity。新版本不能只凭像素稳定复用旧输入。
- `B/native_layout.rs:221–296` 写 raw slot，不执行 LPFR publication；发布仍由观察验证与既有原子协议负责。RGBA 导出与 raw frame 之间还存在 alpha 表示差异，需明确验证。
- Rust renderer 的准备/动画/媒体入口比当前 native adapter 的调用更丰富；整个接管清单不能被该 adapter 的较窄参数裁剪。

## 4. 已有测试与证据缺口

- [tests/README.md](../../../tests/README.md)、[cases.json](../../../tests/web/cases.json)：初次盘点为 8 个自有网页用例；首批迁移新增实体/PNG 后为 10 个，仍不是 WPT-compatible。
- `tests/run_web.py:260` 当前固定 `runtime="mioa"`；仅换 binary 参数不能验证 obscura 身份的 renderer。D0 须补显式 runtime/启动/回执适配，原 renderer CLI/CDP 用例另行保留。
- 2026-11-07 初次架构讨论曾列出 8 项，并单独运行 native-frame-contract：PASS，2784 ms；当时未重跑全部网页/原生内部测试。此历史记录不能作为 Zig 候选或完整冻结基线。
- Parser 原有 fixtures：`browser/tests/{domparser.html,url.html,document/write.html,mutation_observer/parser_chunks.html,shadowroot/declarative.html}`，以及 custom_elements、encoding、page/encoding、script/prescan 相关用例；根路径均为 `runtime/engine/src/`。
- Canvas/GL 原有 fixtures：`runtime/engine/src/browser/tests/canvas/`；资源/字体/动画/打印另有大量 Rust 内部用例，当前小型 native suite 尚未覆盖。
- `F/pdf-print-media.html` 引用 `pdf-print-import.css`；盘点未找到该 CSS 文件。D0 先核实并补全受控 fixture 资源或记录该场景无法运行，不能对缺资源的页面记通过。
- WPT 尚未接入；新的清单必须记录 upstream revision、test ID、标准缺口、raw status、expected metadata 与 regression verdict。旧已知 FAIL 不变不算规范 PASS；既有 PASS 变失败阻断迁移。
- 实施前需为代表性真实网页冻结可合法保存的资源/输入/关键操作/语义结果，保持公开文档无用户 cookie/凭据/私人网页。实时站点 canary 与可复现回归分开。
- 2026-11-07 执行准备追加：现有 engine 参考制品上的 Core/Headless 85/85、harness 8/8、真实网页 8/8 通过；四个故意失败控制保持 FAIL/FAIL/TIMEOUT/ERROR 且退出非零。两个已有制品 SHA 与构建回执匹配，1,825 个 runtime 源文件逐文件匹配；未重建制品、未验完整 renderer/WPT/全部语义/性能，不能勾选完整 D0。

## 5. 外部权威与候选

- [HTML parsing](https://html.spec.whatwg.org/multipage/parsing.html)、[Encoding](https://encoding.spec.whatwg.org/)、[URL](https://url.spec.whatwg.org/)、[UTS#46](https://www.unicode.org/reports/tr46/)：算法与数据权威；实施时冻结适用版本。
- [WPT JS](https://web-platform-tests.org/writing-tests/testharness.html)、[reftests](https://web-platform-tests.org/writing-tests/reftests.html)、[wptrunner](https://web-platform-tests.org/tools/wptrunner/docs/design.html)：复用测试、服务器、manifest、执行模型；CDP 可作为产品 executor 基础，不要求先做完整 WebDriver。
- [Servo testing](https://book.servo.org/contributing/testing.html)：标准测试与产品专有测试分离的现成范式，不复制其工程框架。
- [SuperHTML](https://github.com/kristoff-it/superhtml)：已核对为 Zig/MIT 的 validator/formatter/LSP，提供 tokenizer/AST；未证明可替代浏览器树构造/脚本重入，只能进入 D1 评估。
- [HarfBuzz](https://github.com/harfbuzz/harfbuzz)、[FreeType](https://freetype.org/)：非 Rust 字体基元候选，尚未选入/锁定/构建/验证；不代表其组合已经复现现有 vendored text 行为。

独立正确性依据：标准场景结果、不同原语写出的参考页、固定字体/像素探针、现有真实 GPU/输入/生命周期用例、可复现站点流程；Chromium/Firefox 差量用于诊断而非自动裁定标准。不能让旧实现、新实现或单一浏览器独自充当全部真值。

## 6. 文档资格与实施前阻断

尚无证据宣称完整纯 Zig parser/layout/text/render 已可替换。按 incremental 对每块完成自身 caller/契约/数据与基线即可实施，不把完整 D0/D1 设为所有独立块的共同前置；整体清退仍要求完整覆盖和对应资格。

首批实体 lookup/共享 RGBA→PNG 已落地并实际验证，具体数据/测试/构建回执见 tasks；不是只落文档。发布 MANIFEST、并行 Graphics 草稿、消费者 pin 与用户 Profile 保持不变，整体/managed/发布资格未通过。

## 7. Graphics 契约与执行准备

同仓 [GOS 契约](../../../mioa-os/api/README.md) 与 [图形共享头](../../../mioa-os/api/graphics/graphics_types.h) 是目标契约来源；内容摘要和职责/准入映射见 [graphics-integration](graphics-integration.md)。当前目录没有完整 Host 服务或稳定 wire ABI，共享值类型不是运行时治理实现。独立参考基线通过不构成 managed 资格，未来 Host 缺失也不阻塞独立 parser/text 模块研发。

## 8. 首批模块基线与结果

实体表全量来自已锁定 web_atoms 0.3.0，2,231 项/106 个 legacy alias 逐项核对通过；Zig lookup 不分配内存，已有 Rust HTML tokenizer 内部实体算法未被此块替代。共享 PNG 采用 Zig stdlib zlib，原生失败边界与真实 2D/WebGL/OffscreenCanvas 输出均验证。

默认 Zig/Zig、Rust/Zig、Zig/Rust 三配置分别完成实际构建和 10 个网页用例；默认新制品另通过 85 项 Core/Headless、14 项 harness，native primitive runner 11 项（含入口）通过。13 个 PNG 的独立 Python CRC/zlib/filter/精确像素检查与 Rust 对照通过；失败 controls 保持非零。对照与新候选分别记录制品/输入摘要，不能据此推断未迁移的其他能力或性能优势。
