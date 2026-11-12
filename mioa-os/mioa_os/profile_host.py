"""Trusted-local Profile process assembly; no implicit Core OS backend."""
import os
from pathlib import Path
import socket
import sys

from .artifact_content import LocalArtifactContent
from .channels import LocalUnixChannels
from .host_files import LocalHostFiles
from .messages import LocalUnixMessages
from . import LocalSecureRandom
from .leases import LocalProfileLeases
from .profile_resource_grant import ResourceGrants


def main() -> None:
    from mioa_core.profile_service import _send, _serve

    control = socket.socket(fileno=int(sys.argv[1]))
    endpoints = [socket.socket(fileno=int(fd)) for fd in sys.argv[4:]]
    try:
        _serve(control, endpoints, sys.argv[2], Path(sys.argv[3]),
               messages=LocalUnixMessages(), leases=LocalProfileLeases(),
               random=LocalSecureRandom(), grants=ResourceGrants(),
               artifact_content=LocalArtifactContent(),
               channels=LocalUnixChannels(), files=LocalHostFiles(),
               ready=lambda channel: _send(channel, {"ready": True, "pid": os.getpid()}))
    except Exception as exc:
        try:
            _send(control, {"ready": False, "error": str(exc)})
        except OSError:
            pass
        raise
    finally:
        for endpoint in endpoints:
            endpoint.close()
        control.close()


if __name__ == "__main__":
    main()
