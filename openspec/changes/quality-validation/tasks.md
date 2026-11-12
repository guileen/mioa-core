# Tasks

- [x] T1 — 固定所有权、报告/状态/门禁契约与 Mind-Test；映射 QV-IDENTITY/STATUS。
- [x] T2 — 接入固定 WHATWG/html5lib/PNG 资产、许可和校验；运行实际原生 lookup/PNG 与 libpng；QV-ENT-DATA/PNG-ORACLE。
- [x] T3 — 至少十个成功编译、被指定语义断言检出的定向变异，保留 baseline/patch/日志；QV-MUTATION。
- [x] T4 — 同输入真实 Mioa/Chromium/Firefox 可重复差分与原始输出；WPT 官方 runner 路径/未执行范围明确；QV-DIFFERENTIAL。
- [x] T5 — 实际缺陷 Fuzz→签名保持 ddmin→明确确认→固定后回归，以及拒绝错误提升；QV-FUZZ。
- [x] T6 — 产品编排与追踪 manifest、G0–G4 独立报告、身份/漏执行/旧制品/异常/超时负向控制；QV-IDENTITY/STATUS/HARNESS。
- [x] T7 — 执行端到端 CLI、自检与必要独立评审，记录具体 PASS/FAIL/NOT_RUN/限制；不把组件或历史制品外推成系统/release 通过。

## Evidence
实现与自检：
- 原生独立 baseline 13/13（11 个实际行为测试，2 个导入入口），完整 WHATWG 2,231 个 exact、7,623 个 prefix-only、2,237 个 no-match；13 个源变异均成功编译并被对应语义测试检出。
- libpng 1.6.59 独立检查固定 PNGSuite 与实际 Zig RGBA 编码。PNG corpus 的 160 zero samples 按原始 `pngtest.c` 排除 alpha 统计；观测到的 decoded SHA 不冒充独立预期。
- Mioa、Chromium 141.0.7390.37、Firefox 142.0.1 各执行相同 86 项观测（80 个 html5lib、5 个显式契约、1 个 Canvas），独立 PNG 解码与逐项结果 PASS。
- 真实 Fuzz-before：冻结旧 Python oracle，16 次输入产生 12 个反例，均属一个非字母 chunk 名缺陷；原始 123 bytes 缩至 82 bytes，保持签名并证明声明关系下 1-minimal。原始/最小输入、观测与 SHA 均保留；显式确认、固定后 replay、同格式公开 fixture/G0 重放已完成。修复后 64 次确定性输入无 finding；不是覆盖引导 fuzz 或普遍无缺陷证明。
- 原生 adapter 首次编译揭示 Zig 0.17 `dupeZ` 兼容错误，已改 `dupeSentinel` 后通过；Fuzz 首次运行暴露按 libpng 错误文字分类的脆弱点，已改稳定退出码（3=库拒绝，1=adapter/resource/I/O，2=usage），不把该工具错误计 finding。
- 端到端私有 `quality/run.py` 最终复验 G0/G1/G2 PASS、G3/G4 NOT_RUN；输入运行前后未变，294 条追踪记录，无缺失声明。Core Python 39 项、产品 verifier 9 项通过；运行结束构建回执/runtime/decoder 和所有 child 身份一致。真实 stale-runtime CLI 与未确认/错误 decoder 提升均正确非零且不写回归。
- 两个独立只读评审发现并已修正：制品运行中替换、错误来源目录、非 PASS 状态归并、丢失原始 fuzz 输入、公开 fixture 无法同入口重放、嵌套记录类型、必需测试清单自推导、native-only 实体漏检、变异误入 test helpers、staging 错误归因。修正由永久回归和真实 full-reviewed CLI 复验；没有宣称独立评审已再次复审。
- 原始证据位于 Core `.local/quality-framework/`（before/fixed fuzz、confirmed-reviewed、负向控制、regression-loop 与 review-resolution）和产品 `.local/quality-framework/`（最终 full-reviewed、differential-first、stale-runtime 控制）。历史失败和此前快照结果保留，不用新 PASS 覆盖。

资格边界：浏览器差分明确使用已绑定构建回执的历史 Mioa 制品；当前源码独立模块是新编译。此结果验证质量链路，不是当前整库或发布资格；不更新消费者/Profile。官方 WPT 产品适配/执行为 NOT_RUN，P1 AFL++/Domato/Atheris、全标准与 sanitizer 未运行。用户接受尚待本轮交付后确认；独立评审另记，不以自检代替。
