# Graphics API：规范引用与 Zig 接管映射

本文件是 Zig 接管设计的一部分，不是新的 GOS API、Host 实现或可用 wire binding。上位权威为同仓 [GOS 契约](../../../mioa-os/api/README.md)、[图形共享值类型](../../../mioa-os/api/graphics/graphics_types.h)，以及尚未资格验证的 [resource-v1 native binding](../../../mioa-os/api/graphics/graphics_resource_v1.md) / [声明头](../../../mioa-os/api/graphics/graphics_resource_v1.h)；发生冲突时先修订本 change，不复制/改写通用契约。

## 1. 规范引用与内容身份

- [依赖与作用域](../../../mioa-os/api/README.md#scope-and-dependency-direction)：Core/provider、local backend 与 Host 各自实现，通用契约不依赖 DOM、Python Core 或 Desktop Shell。
- [共享值类型](../../../mioa-os/api/README.md#shared-value-types)：`mg_lease_key`、`mg_gpu_image/plane`、`mg_gpu_limits/layout` 是 C11 源级值，不是授权、wire layout 或稳定 ABI。
- [服务边界](../../../mioa-os/api/README.md#service-boundary)：连接、生命周期、能力、预算、I/O/子任务、binding、输入、内容与释放的统一语义。
- [GPU 数据面](../../../mioa-os/api/README.md#gpu-and-presentation-data-plane) 与 [Canvas/WebGL 归属](../../../mioa-os/api/README.md#webgl-canvas-and-core)：Web 语义留在 Core，通用治理由 GOS 持有，图形执行与呈现由 Graphic 实现。
- [实现资格](../../../mioa-os/api/README.md#evolution-and-implementation-qualification)：具体 binding 先定义操作、字段、枚举、编码/协商/错误，再跑互操作；同名结构或 API 文本不能证明隔离/配额/GPU 完成。
- [resource-v1](../../../mioa-os/api/graphics/graphics_resource_v1.md)：64-bit little-endian macOS/Linux 的局部 native C11 资源接口，版本/操作/状态由 [声明头](../../../mioa-os/api/graphics/graphics_resource_v1.h) 定义；不是完整 Graphic 服务或 wire format，尚未取得实现/兼容资格。引用其 Linux 目标不扩张当前 Core 的 macOS arm64 资格。

2026-11-07 准备检查采用的历史路径和内容摘要（非迁移后身份）：

| 源文件 | SHA-256 |
|---|---|
| `graphics-api/README.md` | `9d5f26bc52ab38a9143ceb8fb88c4fea1d2ef8960005b48bae74b16335c336a3` |
| `graphics-api/graphics_types.h` | `7bcc244c487a22300ab07a63f936c0eda332ae74452ebdbd9d8045ecb3ff0ac1` |
| `graphics-api/graphics_resource_v1.md` | `c1ad25f1a074de715b2054b29ad29f42976f5bec71155553c2f7984f93e4d16b` |
| `graphics-api/graphics_resource_v1.h` | `6f2f82da350331b796178b9b1b5fd1c94b3bea997eb80a8271d078e98d0ff85d` |

这是已核对的工作树内容快照，不是发布 pin；resource-v1 已声明版本也不等于已通过 native ABI 资格。准备中检测到 README 并行新增 resource-v1 后，已重新读取新增文档/头并更新这四份摘要。D0/D1 与正式候选必须重新校验内容身份；上位契约变化时重新评估受影响映射，不能默默沿用旧摘要。正式消费者使用批准提交和内容闭包，不以可变工作树摘要代替发布。

## 2. 职责与调用映射

| Graphics 契约族 | Core/provider 侧归属 | 外部实现责任 / 不变量 |
|---|---|---|
| Connect / capabilities | runtime 集成记录连接代次、提供/必需能力、实际执行域 | assembler 绑定实际 backend；认证身份不从 payload ID 推断；必需能力不足先拒绝，不创建有副作用的对象 |
| Lifecycle | Zig session/document/task owner 执行 start/quiesce/resume/stop，报告实际状态 | Host 管应用/执行域生命周期；停止确认不等于进程/子任务/资源已结清，Tab 不自动等于 ExecutionDomain |
| Capabilities | Web 权限与 input/control 最终使用前验证，管理撤销 epoch | Host 授予/撤销通用权利；focus 和 lease key 本身不是授权，已派发副作用不能被撤销抹掉 |
| Execution / budget | 将文档、Worker、资源、CPU/GPU 工作归属请求 scope，按实际支持强度执行 | Host 分配上限/保留/权重/期限；子额度不得超父级，声称硬限制需真实执行域证据，无能力拒绝该配置 |
| Memory / I/O / children | 分配/解码/网络/Worker 创建前按授予预算预留；追踪取消、完成或未知 | 实现方管理队列/计费/执行约束；不能每个 Canvas API 都远程往返授权，批量下发不取消最终验证 |
| Surface / View / Binding | provider 管网页内容与 confirmed binding revision；只有匹配几何可交互 | 有权配置者单写；View 是显示附着，不是 DOM/Canvas；镜像不能增加配置 writer |
| Input | Web adapter 映射 binding、focus/capture epoch、坐标/呈现依据与 actor；Core 做 hit-test、stale/control/Web 权限校验 | Host 做路由与系统焦点/capture；路由成功、provider 执行与未知分别报告，不静默重放非幂等输入 |
| Content | RenderSession 发布 coherent snapshot、damage baseline、格式/范围/ready 与 lease/use 关系 | backend/Host 验证接收、提交、完成和呈现；这四个事实不混用，不独立叠加破坏页面裁剪/混合的 Canvas |
| Release / device | Core 跟踪仍在使用的 Canvas/图像/字体/帧代次，映射 Web context loss | 完成来源可信且按 use/generation 幂等；close/断连/超时/device change 都不等于完成，晚到事件不释放新资源 |

通用资源账本和 Host scheduler 归 GOS；registry/compositor 属 Graphic；平台后端负责适配。公共契约不持有实现。Web adapter 可在 Core 侧导出 Web 契约，在产品组装处映射通用契约；不让通用 Host 导入 Web Core 或私有 Shell。

## 3. Zig 类型与数据边界

- 公共 C11 头是共享值类型的唯一来源。Zig 用构建期受控的 C import/translate 获取声明，不维护另一套同名布局/常量；它不引入 Rust 或现成执行服务。
- `mg_lease_key` 的 scope/resource/generation 是身份，不是可直接调用的 OS handle 或 capability。`mg_gpu_image` 是线性图像元数据，不包含完整授权、句柄转移或 readiness/completion 协议。
- C enum/struct 的内存布局不能直接用于序列化。具体 binding 需定义端序、长度、枚举、格式/alpha/色彩空间、认证引用、同步与错误；未知表示先拒绝，不猜 stride、plane、device 或权限。
- 当前支持 R8/RG8/RGBA8/BGRA8/NV12 线性交换不限制 WebGL/Canvas 语义。其他 tiled/compressed/depth/3D 内容使用经资格验证的单独 binding，不强塞到 `mg_gpu_image`。
- 像素平面进行 checked arithmetic、alignment/limits/range/rights 验证；有效内存范围不代表拥有资源访问权，元数据合法也不证明 producer 已完成。
- Web 内部 `SnapshotKey` 与外部 connection/surface/binding/content/use/device 代次分别关联；不得把 DOM node ID、裸 PID、像素 hash 或 LPFR slot number 直接当作外部授权身份。
- resource-v1 采用其已声明的 `mg_resource_call_v1`，不另发明资源服务入口。endpoint/非零 epoch 由可信 Host 提供，client 不构造 endpoint、选择 principal、注入 backend 或宣称 GPU 完成；Host 序列化调用/CPU END，owner 销毁后禁止再用 endpoint。
- native request/response 必须独立、8-byte 对齐；版本、struct_size、4096-byte 上限、reserved/扩展与 response prefix 按声明验证。失败不得改 response。CPU borrow 到匹配 END 截止，撤销后仍结清已接受 use；PENDING、close 与 STATUS 都不被当作工作已完成。publication 封闭写入，CPU 读仅在 PUBLISHED 且发布的只读保护成功后允许；native GPU image 不暴露 CPU 指针。
- `MG_GPU_*` 与 `MG_RESOURCE_FORMAT_*` 的数值空间不同（resource-v1 另有 FORMAT_NONE），必须按具名格式显式映射；`mg_lease_key` 与 `mg_resource_handle_v1` 也不得仅因大小相同直接互转为授权。value types 不是 wire/native binding 类型的替代。

实现时 adapter 至少需要表达四组语义记录：authenticated connection/authority、confirmed binding/configuration、content/resource readiness、consumer use/completion。它们是对上位语义的映射，不是本文件另定的稳定结构体或新 wire API；具体字段/编码必须由 D1 binding 决策和互操作证据确定。

## 4. Standalone 与 managed 的边界

Standalone Headless 可使用本地 backend，不加载 Desktop/Graphics 服务；保留现有 CDP/stdio/UDS/LPFR v2。当前 CPU/LPFR 及显式 screenshot/打印行为不因目标 GPU 契约被删除，也不因接管要求凭空新增 GPU 展示后端。

Managed 会话必须受其 governing Host 的 authority/budget/binding 约束；同会话的 Headless/本地 backend 不是逃逸通道。缺少可信 Host 或具体 binding 时，不能把 standalone smoke 宣称 managed 资格；需要该能力的配置明确拒绝，不使用假 Host/no-op quota 通过验收。

未来协商 GPU resident 展示时避免无谓 readback/re-upload；当前 CPU 路径必要 readback 单独归属、计费与验证。已接受按键/按钮转换和副作用请求不能被 latest-frame 合并策略丢弃，控制/终止容量保留在总预算内。

## 5. 接入与执行准入

- D0：记录契约内容身份、现有 Web/LPFR 路径和受影响调用者；分别列出 standalone 已有证据与 managed 尚缺实现/互操作证据。完整基线未冻结时，不宣布接管基线完成。
- D1：锁定实际类型导入、binding/能力协商、输入/内容/资源代次映射与失败值。资源子集优先复用 resource-v1 已声明 native 接口并取证，不另造接口；完整服务/数据面仍无稳定 wire ABI，不能假定 Host 实现或 managed 资格存在。
- D5/D6：真实测试 stale/revoked authority、binding resize/focus 竞态、队列耗尽/停止、在途断连/device loss、重复/晚到完成；保留 Web final authorization、独立 Headless 和真实像素/输入行为。
- D7：将上述结果绑定到批准的 Core/契约/backend/消费者修订。managed 需要实际实现方配合；缺少该实现不阻止独立 parser/text 等模块研发，但阻止 managed 接入通过和整体资格声明。

验收场景见 [Z13–Z15](specs/zig-native-web-core/spec.md)。本次准备不会实现新 Graphics 服务、修改共享头、替换原生链路或改变用户 Profile。
