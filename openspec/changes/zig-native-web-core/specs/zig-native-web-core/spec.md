## ADDED Requirements

### Requirement: Zig-owned native implementation and complete scope

第一方原生 Web 语义、调度与集成 SHALL 由 Zig 实现；最终项目控制的 engine/renderer 原生生产依赖闭包 SHALL 不含 Rust 库、Cargo 构建、Rust 子进程或动态回退。V8、批准的 C/C++/系统基元与 Python/Node 控制工具 SHALL 单独列明，不得宣称整个浏览器及工具链纯 Zig。所有现有 Rust 生产入口及传递依赖职责 SHALL 在接管账中有目的地与行为证据。

#### Scenario: Z1 Clean build without a Rust native closure
- **WHEN** 在隔离输出根中，禁用 Cargo/rustc 和旧 Rust 制品，构建并启动 engine 与 renderer 目标
- **THEN** 两者由声明的 Zig/非 Rust 基元输入完成构建，真实页面、控制和像素路径可用，编译/链接/运行依赖回执无未申报 Rust 输入；不是只检查文件扩展名或 Cargo 命令是否出现。

### Requirement: Browser-grade parsing, encodings and URLs

Zig 接管 SHALL 保留现有文档/上下文片段/流式 HTML、脚本重入、模板与 Shadow DOM、XML、实体、资源预扫描、字符编码、TextDecoder、表单编码、WHATWG URL 与 UTS#46 IDNA 的可观察语义。HTML 恢复性解析错误 SHALL 与 OOM/取消等运行失败分开。已知旧错误 SHALL 依据标准与独立用例修正，不以旧输出作为唯一真值。

#### Scenario: Z2 Incremental parse, mutation and teardown
- **WHEN** HTML 在多字节字符中间分块，执行 document.write、定制元素回调、节点重挂载或导航取消
- **THEN** 输出与回调顺序符合选定标准场景，无递归喂入同一解析器、字节静默丢弃、重复脚本执行、跨文档节点误用或释放后回调；XML 和上下文片段有独立验收。

#### Scenario: Z3 Encoding and URL authority
- **WHEN** TextDecoder 执行 streaming/fatal/BOM/flush，旧编码参与导航或表单，URL 经 base/组件 setter/序列化及 Unicode 主机转换
- **THEN** 编码状态、消费长度、错误分类、URL 组件与 origin 结果满足固定的标准/既有语义用例；URL、DNS/连接策略与页面公开 API 使用同一规范化权威，std.Uri 或 punycode 单独通过不算 WHATWG/UTS#46 接管。

### Requirement: Rendering parity beyond the current native adapter

迁移 SHALL 保留当前 engine 集成与 standalone renderer 已有能力，包括 CSS 级联/变量/生成内容、现有块/行内/flex/grid/table/float 布局、文字与字体、图片/SVG、变换/裁剪/层叠、滚动/子帧、动画、屏幕/打印。源码存在、测试存在、实际通过 SHALL 分开记录。没有证据的媒体解码或任意属性动画支持 SHALL 不凭此规格宣称实现。

#### Scenario: Z4 Existing rendering corpus and real pages
- **WHEN** 同一输入、字体、viewport/DPR、媒体模式、资源响应和受控时钟运行迁移前基线与 Zig 候选
- **THEN** 全部已验证能力无未解释回归，布局与像素依据标准/独立参考验收；字体、SVG、动画、资源迟到和打印等未被当前 8 个网页用例覆盖的能力也有逐项结果，不能靠删除用例或放宽全局阈值通过。

### Requirement: Canvas and WebGL remain Web Core responsibilities

Canvas 2D、OffscreenCanvas、WebGL 的现有状态、验证、绘制、纹理上传、readback、线程/Worker 所有权及上下文丢失语义 SHALL 由 Zig Web 模块接管。通用 Host SHALL 不接管 Web API 状态或绕过网页裁剪/层叠。CPU/GPU、premultiplied/straight alpha 和颜色空间 SHALL 明确区分。

#### Scenario: Z5 Surface state and lifecycle
- **WHEN** 页面绘制半透明内容、上传子纹理、读取非零原点区域、resize、transfer/close 或在 Worker 与页面间交接表面
- **THEN** 实际像素、状态重置/保留、范围验证与线程约束符合各 API 既有有效行为；不能返回占位像素、空成功或把必需 GPU 行为静默降级。

### Requirement: One document and coherent render snapshots

活 DOM SHALL 只有现有 Zig Document 一个可变语义权威；布局树、文字结果和绘制缓存可以派生，但 SHALL 带文档/节点代次及版本。几何查询、命中、caret、绘制、帧指纹和输入校验 SHALL 使用相容的快照、滚动/变换与坐标空间。最终正常热路径 SHALL 不为缓存命中检查而重建整树 JSON 或另一套可变 DOM。

#### Scenario: Z6 Same pixels, different identity
- **WHEN** 导航后画面相同，或子帧、字体、资源、焦点、选择、动画与滚动改变
- **THEN** 文档身份与必要版本仍更新，过期观察上的输入在副作用前拒绝；当前命中与 caret 对应真实绘制位置，不能以像素相同证明旧引用仍有效。

### Requirement: Explicit resources, completion and bounded work

缓冲区、字体、图片、Canvas/GPU 对象与导出帧 SHALL 有唯一所有者和可验证借用/租约期限。失效只阻止新使用，不代表外部使用完成。尺寸、stride、长度、预算与输入算术 SHALL 检查溢出；失败 SHALL 不发布半初始化对象。共享可变后端 SHALL 使用实际线程所有权或同步，不声称天然无锁。

#### Scenario: Z7 Failure with in-flight work
- **WHEN** OOM、取消、队列满、document close、连接断开或 device loss 与正在读取的帧/资源相遇
- **THEN** 新工作被拒绝或终止且原因可见，已派发工作记录完成/失败/未知，只有实际使用结清或安全终止后释放；重复完成不释放新代次资源，LPFR 写入与发布仍是不同动作。

### Requirement: Independent frontends and stable consumer contracts

engine/renderer 两个现有构建目标及其实际使用的 CLI/CDP/服务能力 SHALL 有 Zig 原生实现与逐项迁移记录。独立 Headless SHALL 不依赖 Desktop 或通用 Graphics 服务；managed 模式 SHALL 不绕过 Host 权限。公开运行时身份、控制协议、LPFR v2 与支持的 API SHALL 保留，必要破坏性变更须先修订契约并迁移所有调用方。

#### Scenario: Z8 Two targets and standalone operation
- **WHEN** 两目标从无兄弟源码的干净候选启动，独立客户端导航、执行脚本、输入、取帧并退出
- **THEN** 各自约定的 CLI/协议能力可用，前端共享同一 Zig 语义实现且不回退到旧二进制，Headless 不启动 Desktop；必需能力缺失为明确失败，不伪装成功。

### Requirement: Reuse standard tests and expose raw outcomes

标准兼容性 SHALL 复用固定版本 WPT 的原始测试、服务器、manifest 与 wptrunner 适配，不批量改写成私有断言格式。Core 特有契约 SHALL 留在现有小型测试入口；有行为价值的 Rust 测试 SHALL 在删除原实现前移植。原始 FAIL/TIMEOUT/CRASH/ERROR、标准缺口、已知预期与回归判定 SHALL 分开报告。

#### Scenario: Z9 Standards and regression gates
- **WHEN** 执行选定 WPT 清单、迁移后的语义用例、现有真实引擎用例和故意失败控制
- **THEN** 服务器/多 origin/TLS/时钟/真实输入条件有记录，空选择或缺能力不算通过；既有通过项不得变失败，旧失败只有按标准证明改进才更新预期，测试分批运行不缩减产品 Web 目标。

### Requirement: Reversible activation and preserved profiles

新制品 SHALL 继续受现有 Profile 租约、身份与 binary/font digest 校验约束。升级 SHALL 显式迁移并保留 id/seed/策略/存储，备份与旧制品受保护。生产回退 SHALL 采用显式兼容性检查，不自动恢复旧数据覆盖升级后新增数据。

#### Scenario: Z10 Reject, migrate, activate and recover
- **WHEN** 新 binary/font 与旧 Profile 不兼容，或迁移/激活中断，或需要回到旧版本
- **THEN** 默认启动先拒绝且保留首因；授权迁移原子提交配置与私有快照，不重置身份；旧版本仅在存储兼容时重开，新旧数据分叉时保留两侧并拒绝破坏性降级。

### Requirement: Source-bound cutover and truthful qualification

最终切换 SHALL 删除被替代的活跃 Rust 源码/依赖/构建分支、旧 FFI 入口与无人使用的别名；Git 历史和许可来源记录 SHALL 保留。锁、MANIFEST、源码闭包、制品回执、两个消费者 pin 与文档 SHALL 同步，且不得夹带并行修改或私人证据。平台、性能和发布资格 SHALL 仅按实际证据声明。

#### Scenario: Z11 Complete release tuple
- **WHEN** 冻结候选、安装两个固定版本消费者并进行真实 Headless/GUI/升级与回滚验收
- **THEN** 每个制品可追溯到批准的完整源码/工具/依赖，未申报输入或篡改被拒绝；无旧 Rust 路径、隐式兄弟源码和运行时兜底；未通过项目继续阻止完整接管声明，许可不因转写或删 Rust 改变。

### Requirement: Explicit implementation authorization and bounded activation

文档/准备阶段 SHALL 不隐含生产切换授权。2026-11-07 用户已明确授权 Rust 加载微调与逐块替换/测试；当前实现 SHALL 按 [incremental](../../incremental.md) 以模块级准入推进，不将完整 WPT 或无关 Graphics Host 作为独立模块开工前置。消费者/用户 Profile 激活仍 SHALL 独立验证并保护已有数据。参考制品通过不等于 Zig 候选通过。

#### Scenario: Z12 Documentation-only delivery
- **WHEN** 仅获得文档授权时交付规范
- **THEN** 文档把当前事实、目标设计、未验能力与实施任务分开，实施任务保持未勾选；结构检查与思想实验不被表述为代码迁移、运行通过或发布资格。

### Requirement: Conformance to the colocated Graphics API

Core 的目标适配 SHALL 以上位 [GOS 契约](../../../../../mioa-os/api/README.md)、[图形共享值类型](../../../../../mioa-os/api/graphics/graphics_types.h) 与 [resource-v1 文档](../../../../../mioa-os/api/graphics/graphics_resource_v1.md)/[声明头](../../../../../mioa-os/api/graphics/graphics_resource_v1.h) 为唯一通用契约来源，按 [接入映射](../../graphics-integration.md) 记录内容身份、能力与职责。Zig 类型 SHALL 从共享声明导入而非另立布局；C 值类型 SHALL 不被当作稳定 wire ABI、认证 capability 或已实现服务。resource-v1 尚未资格验证，资源接口与完整 Graphic 服务资格 SHALL 分开。

#### Scenario: Z13 Contract identity and authority-aware admission
- **WHEN** 引用契约发生变动，或会话请求 managed 能力、资源范围和 binding 配置
- **THEN** 先验证契约/所需能力/认证作用域并更新映射，未经确认的几何不可交互，lease key/焦点/裸 ID 不授予权利；缺失必需执行约束在副作用前明确拒绝，不靠复制契约或 no-op 约束通过。

#### Scenario: Z14 Standalone independence without managed escape
- **WHEN** 独立 Headless 使用 local backend，或受 Host 治理的会话通过 Headless/本地入口发起工作
- **THEN** 前者不加载 Desktop/Graphics 服务并保持已有协议能力，后者仍遵守其 governing authority/budget；不存在实际 Host/binding 证据时不宣称 managed 通过，也不阻止无关独立模块研发。

#### Scenario: Z15 Content, input and resource completion mapping
- **WHEN** resize/focus/撤销、队列耗尽、断连/device loss、重复或晚到 completion 与在途帧/输入交错
- **THEN** adapter 保持 binding/content/use/device 代次与 Web snapshot 关联，最终 hit-test/授权仍在 Core；accepted/submitted/completed/presented 分开，已接受输入不因合帧丢弃，实际使用结清前不释放资源；resource-v1 的 endpoint/epoch、borrow/END、publication/PENDING 与失败 response 不变约束被保留，两个头的格式枚举具名映射，C 图像元数据合法不被误认为可访问或已 ready。

### Requirement: Single static library and independently selected modules

逐块迁移 SHALL 继续只链接一个 Rust staticlib，模块 provider 在编译期固定并进入构建回执。Zig 入口生效时对应 Rust export SHALL 不存在；尚未迁移的模块继续使用 Rust 不构成回退。只有已实现完整契约的模块可以选择 Zig；测试与声明 SHALL 按真实模块边界报告。

#### Scenario: Z16 Static mixed implementation without fallback
- **WHEN** 以不同已实现模块 provider 组合构建并运行真实页面与模块测试
- **THEN** 每个入口只采用记录的实现，无符号冲突、运行时回退或双副作用；各实现遵守相同行为/错误边界，未迁移能力仍可用，任一块失败不被整体 PASS 或其他块结果覆盖。
