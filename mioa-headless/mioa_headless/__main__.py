"""Command entrypoint; --help needs neither the manager nor a native runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .service import ServiceError, default_home, run_service, safe_error, validate_url


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse can echo arbitrary option values, including credentials.
        raise ServiceError("invalid_config", "Invalid command arguments; use --help for supported options.")


def parser() -> argparse.ArgumentParser:
    result = _Parser(description="Own one persistent Mioa session over local stdin/stdout NDJSON.",
                     epilog="The current native runtime requires macOS CGL and a Unix profile manager. "
                            "Linux/Windows native execution is not supported. No browser is downloaded.")
    result.add_argument("--profile", required=True, help="Independent persistent profile name")
    result.add_argument("--home", default=None, help=f"Profile application-data root (default: {default_home()})")
    result.add_argument("--binary", default=None, help="Pinned Mioa executable; used when creating a profile")
    result.add_argument("--font-file", action="append", default=None, help="Actual font file to pin; repeat for multiple files")
    result.add_argument("--url", default=None, help="Initial HTTP(S) URL or about:blank; no external egress probe is performed")
    result.add_argument("--allow-private-network", action="store_true", default=None,
                        help="Allow private-network destinations in a new profile; existing policy must match")
    result.add_argument("--native-socket", default=None, help="Private native UDS control path (requires --frame-shm)")
    result.add_argument("--frame-shm", default=None, help="LPFR v2 raw frame file in the same mode-0700 session directory")
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        args = parser().parse_args(argv)
        if (args.native_socket is None) != (args.frame_shm is None):
            raise ServiceError("invalid_config", "--native-socket and --frame-shm must be supplied together.")
        if args.url is not None:
            args.url = validate_url(args.url)
        return asyncio.run(run_service(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Never expose native logs, environment configuration or Python tracebacks.
        error = safe_error(exc, startup=True)
        try:
            sys.stdout.write(json.dumps({"event": "error", "error": error}, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
