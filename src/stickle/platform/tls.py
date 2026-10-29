"""Checking HTTPS certificates with the system's own authorities.

Windows and macOS: the system checks them itself (truststore). Python's own
check reads only the authorities already in the system's store, and Windows
adds one that is missing only when its own check needs it, so on a newly set up
Windows the website's could not be checked. Linux: the OpenSSL bundled with
Stickle looks for authorities where the system it was built on keeps them,
which other distributions do not share, so the usual places are tried in turn
(as Go and others do). Nothing is installed or bundled for it.
"""

import ssl
import sys
from pathlib import Path

LINUX_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Mint, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/tls/cacert.pem",  # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # CentOS, RHEL 7
    "/etc/ssl/cert.pem",  # Alpine
)


def https_context() -> ssl.SSLContext:
    if sys.platform == "win32" or sys.platform == "darwin":
        # Here, not at the top: only the process that fetches loads it.
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context = ssl.create_default_context()
    if sys.platform.startswith("linux") and not context.get_ca_certs():
        for bundle in LINUX_BUNDLES:
            if Path(bundle).is_file():
                try:
                    context.load_verify_locations(cafile=bundle)
                except OSError, ssl.SSLError:
                    continue
                break
    return context
