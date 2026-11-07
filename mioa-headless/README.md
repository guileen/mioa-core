# mioa-headless 0.1.0

Local Mioa session service for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Provides the `mioa-headless` console command and `python -m mioa_headless`.
Stdio NDJSON methods: `state`, `frame`, `control`, `action`, `axtree`,
`shutdown`; the optional native UDS transport adds `resize` and `stop`. The
service owns one native runtime, exposes the bounded Agent `axtree` observation,
and rebinds pointer input to the live render when an animation changed the
pixels after capture.

Depends on `mioa-core==0.1.0`. No native runtime, font, UI or product CLI is
included. The tests in `tests/` exercise the service without a native runtime.
