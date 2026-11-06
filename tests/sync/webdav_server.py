"""A WebDAV server in the test (wsgidav, a development dependency only), for
the tests that talk real HTTP: over plain HTTP or TLS with a test certificate
authority (trustme), open to all or behind a password."""

import http.client
import ssl
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import trustme
from cheroot import wsgi
from cheroot.ssl.builtin import BuiltinSSLAdapter
from wsgidav.wsgidav_app import WsgiDAVApp  # pyright: ignore[reportMissingTypeStubs]

HOST = "127.0.0.1"


@dataclass(frozen=True)
class Server:
    port: int
    context: ssl.SSLContext | None  # trusting its certificate, over TLS; None over HTTP

    def connection(self, context: ssl.SSLContext | None = None) -> http.client.HTTPConnection:
        """A connection to it: over TLS, trusting context (its own unless given)."""
        if self.context is None:
            return http.client.HTTPConnection(HOST, self.port, timeout=10)
        return http.client.HTTPSConnection(
            HOST, self.port, timeout=10, context=context or self.context
        )


@contextmanager
def webdav(
    root: Path, password: str | None = None, ca: trustme.CA | None = None
) -> Generator[Server]:
    """Serving root while in the with block. password: for the user "stickle"
    (None: no sign-in at all). ca: over TLS, with a certificate it issued."""
    users: object = True if password is None else {"stickle": {"password": password}}
    app = WsgiDAVApp(
        {
            "provider_mapping": {"/": str(root)},
            "simple_dc": {"user_mapping": {"*": users}},
            "http_authenticator": {
                "accept_basic": True,
                "accept_digest": False,
                "default_to_digest": False,
            },
            "verbose": 0,
            "logging": {"enable": False},
        }
    )
    server = wsgi.Server((HOST, 0), app)
    context = None
    if ca is not None:
        leaf = ca.issue_cert(HOST)
        chain = root.parent / f"{root.name}-cert.pem"
        key = root.parent / f"{root.name}-key.pem"
        leaf.cert_chain_pems[0].write_to_path(chain)
        leaf.private_key_pem.write_to_path(key)
        server.ssl_adapter = BuiltinSSLAdapter(str(chain), str(key))
        context = ssl.create_default_context()
        ca.configure_trust(context)  # pyright: ignore[reportUnknownMemberType]
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    port = cast(tuple[str, int], server.bind_addr)[1]  # pyright: ignore[reportUnknownMemberType]
    try:
        yield Server(port, context)
    finally:
        server.stop()
        thread.join(10)
