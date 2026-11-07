# mioa-core 0.1.0

Shared Mioa core library for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Public modules: `profile` (profile/state storage, leases, strict psutil process
identity, proxy secrets, archives), `native_spec` (schema-3 runtime config and
digests), `native` (bounded CDP client and rebound guarded input), `refs`
(bounded DOM snapshot and opaque references), `operations` (declared operation
table, including `tools.axtree`), `control` (ownership/observation lease),
`engine` (owned headless engine), and `net` (proxy parsing and egress probes).

Hard dependencies: `psutil==7.2.2`, `requests==2.34.2`. No native runtime,
font, UI or product CLI is included.
