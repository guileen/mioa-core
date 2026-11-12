# Resource service v1

`graphics_resource_v1.h` declares a native C11 interface for qualified 64-bit little-endian macOS/Linux targets; it is not a wire format. Implementations qualify their behavior and compatibility against this binding. The full Graphics service is a separate architecture-level specification.

A trusted Host issues an endpoint and nonzero epoch and serializes calls and CPU finishes. Endpoint storage remains valid until that Host destroys its owner. The client never constructs endpoints, selects a principal, asserts GPU completion, or injects a backend. Handles identify objects but confer no rights. Never use an endpoint after owner destruction; borrowed pointers are valid only until their matching END.

Negotiate before creation, begin or publication. The first successful negotiation fixes features and device for that epoch; subsequent required features outside that selection are unsupported. Unsupported optional features are omitted. Revocation refuses new work but preserves matching END for accepted uses. Close retires an object; PENDING means work or disposal remains, not completion. STATUS may perform bounded nonblocking collection. Released handles become stale when their slots are reused. Publication seals writes; CPU reads require PUBLISHED, and CPU publication requires successful read-only protection. A native image has no public CPU pointer.

| Operation | Required state and effect |
|---|---|
| NEGOTIATE | Select supported required and optional features; return the selected device or a null device. |
| CREATE | Require Host create authority and requested access; return a handle only after backing allocation and accounting succeed. CPU buffers specify a nonzero byte length; images specify dimensions, supported format and negotiated device. |
| WRITE_BEGIN / WRITE_END | A single bounded CPU writer may borrow a WRITABLE buffer. END releases that use after actual access ends and does not publish. |
| PUBLISH | Require Host publish authority. CPU publication requires no writer and seals the mapping read-only; image publication requires an accepted producer and readiness association. Repeating publication is OK. |
| READ_BEGIN / READ_END | Borrow a nonzero in-bounds range of a PUBLISHED CPU buffer with READ access; END releases that use after actual access ends. A duplicate END is stale. |
| DESCRIBE | Return immutable accepted metadata and current state without granting access. CPU allocation bytes are the actual page-rounded extent; image planes describe only qualified linear native layouts. |
| CLOSE / STATUS | CLOSE requires Host close authority and retires the resource; both perform bounded collection. Return PENDING while uses or disposal remain and OK once released. Repeated close of the matching released generation is OK. |

Images do not support CPU BEGIN. Unknown access bits and nonzero reserved fields are invalid; feature bits do not authorize operations. A resource handle cannot be used by another endpoint without authority. END remains available after close or revoke only to the endpoint that accepted the use.

All requests and responses are disjoint, valid, 8-byte-aligned native storage. Set request `struct_size` to the supplied byte count, at least the opcode prefix and at most 4096. Use version `MG_RESOURCE_V1_VERSION`. Reserved fields must be zero; zero extension bytes are accepted, nonzero unnegotiated extensions are unsupported. Supply enough response capacity for the known prefix. OK and PENDING write the known response prefix; failures leave response bytes unchanged. Unknown opcodes, major versions and required features return UNSUPPORTED; malformed sizes return INVALID. The constants, field types, sizes, states and operation declarations are in the header.

V1's qualified layout, status values, ownership and behavior are stable. Optional append-only minor additions require feature negotiation; incompatible changes require a new major entry point. The existing `graphics_types.h` source-level values retain their original meanings and are not a stable binary ABI. Neither this local trusted binding nor handle validation is a sandbox.
