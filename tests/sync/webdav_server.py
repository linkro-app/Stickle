"""A WebDAV server in the test (wsgidav, a development dependency only), for
the tests that talk real HTTP: over plain HTTP or TLS with a test certificate
authority (trustme) or a certificate signed by itself, open to all or behind
a password. It notes the requests that reach it."""

import datetime
import http.client
import ssl
import threading
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import trustme
from cheroot import wsgi
from cheroot.ssl.builtin import BuiltinSSLAdapter
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from wsgidav.wsgidav_app import WsgiDAVApp  # pyright: ignore[reportMissingTypeStubs]

HOST = "127.0.0.1"


@dataclass(frozen=True)
class Asked:
    """A request that reached the server."""

    method: str
    path: str
    sign_in: str | None  # its Authorization header


@dataclass(frozen=True)
class Server:
    port: int
    context: ssl.SSLContext | None  # trusting its certificate, over TLS; None over HTTP
    certificate: bytes | None = None  # the one it shows (DER), over TLS
    asked: list[Asked] = field(default_factory=list[Asked])

    def connection(self, context: ssl.SSLContext | None = None) -> http.client.HTTPConnection:
        """A connection to it: over TLS, trusting context (its own unless given)."""
        if self.context is None:
            return http.client.HTTPConnection(HOST, self.port, timeout=10)
        return http.client.HTTPSConnection(
            HOST, self.port, timeout=10, context=context or self.context
        )


def self_signed() -> tuple[bytes, bytes]:
    """A certificate signed by itself, of a name that is not the server's address,
    as a NAS comes with (PEM), and its private key (PEM)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "my-nas")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


type _Start = Callable[..., Any]
type _App = Callable[[dict[str, Any], _Start], Iterable[bytes]]


def _noting(app: _App, asked: list[Asked]) -> _App:
    def noted(environ: dict[str, Any], start: _Start) -> Iterable[bytes]:
        asked.append(
            Asked(
                str(environ["REQUEST_METHOD"]),
                str(environ["PATH_INFO"]),
                cast(str | None, environ.get("HTTP_AUTHORIZATION")),
            )
        )
        return app(environ, start)

    return noted


@contextmanager
def webdav(
    root: Path,
    password: str | None = None,
    ca: trustme.CA | None = None,
    *,
    own: tuple[bytes, bytes] | None = None,
    name: str = HOST,
    not_after: datetime.datetime | None = None,
) -> Generator[Server]:
    """Serving root while in the with block. password: for the user "stickle"
    (None: no sign-in at all). ca: over TLS, with a certificate it issued, for
    name, until not_after. own: over TLS, with this certificate and key (PEM)."""
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
    asked: list[Asked] = []
    server = wsgi.Server((HOST, 0), _noting(cast(_App, app), asked))
    context = None
    certificate = None
    if ca is not None or own is not None:
        chain_file = root.parent / f"{root.name}-cert.pem"
        key_file = root.parent / f"{root.name}-key.pem"
        if ca is not None:
            leaf = ca.issue_cert(name, not_after=not_after)
            chain = leaf.cert_chain_pems[0].bytes()
            key = leaf.private_key_pem.bytes()
        else:
            assert own is not None
            chain, key = own
        chain_file.write_bytes(chain)
        key_file.write_bytes(key)
        certificate = ssl.PEM_cert_to_DER_cert(chain.decode("ascii"))
        server.ssl_adapter = BuiltinSSLAdapter(str(chain_file), str(key_file))
        context = ssl.create_default_context()
        if ca is not None:
            ca.configure_trust(context)  # pyright: ignore[reportUnknownMemberType]
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    port = cast(tuple[str, int], server.bind_addr)[1]  # pyright: ignore[reportUnknownMemberType]
    try:
        yield Server(port, context, certificate, asked)
    finally:
        server.stop()
        thread.join(10)
