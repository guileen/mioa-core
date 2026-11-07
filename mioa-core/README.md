# mioa-core 0.1.0

Shared Mioa core library for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Public modules: `profile` (profile/state storage, leases, strict psutil process
identity, proxy secrets, archives), `native_spec` (schema-3 runtime config and
digests), `native` (bounded CDP client and rebound guarded input), `refs`
(bounded DOM snapshot and opaque references), `operations` (declared operation
table, including `tools.axtree`), `control` (ownership/observation lease),
`engine` (owned headless engine), and `net` (proxy parsing and egress probes).

Hard dependencies: `psutil==7.2.2`, `requests==2.34.2`. This Python package does
not embed native executables or Desktop UI. The owning repository maintains
native webpage execution/layout/paint sources and fonts under `runtime/`,
with its locked builder and compact browser tests. Native component licenses
are separate from this Python package's MIT license.

Page startup requires only the basic page/network/runtime protocol. Site-icon
subscription starts on the first `NativePage.site_icon()` call; a backend
without that extension reports `capability_unavailable` for the icon request,
not for ordinary navigation or rendering.
