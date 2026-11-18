"""Access tokens from a refresh token, for the clouds signed in to with OAuth.

A refresh token is all a device keeps of a sign-in (in a table of its own local
database). The access token made from it lives in memory only, and another is
made when it is about to run out or the cloud turns it down. A cloud may answer
with a new refresh token (Microsoft does, each time): the one before keeps
working for a while, but the new one is told at once, to be kept instead.

Who is talked to: the cloud's token address only, over https, with a
certificate a public authority issued for it. A redirect is not followed; the
refresh token goes nowhere else.

A refresh turned down for good (the user took the app's access away, changed
their password, or let the token run out) is SignInNeededError: it is not tried
again, here or by anyone above. The app itself turned away is NoAccessError.

Nothing here logs, and no error, nor the repr, says what a token is.
"""

import http.client
import json
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

from stickle.core.cloud_time import date_millis
from stickle.platform.tls import https_context
from stickle.sync.targets.base import (
    NoAccessError,
    SignInNeededError,
    TargetError,
    TooBusyError,
    UnavailableError,
)

TIMEOUT = 30.0  # seconds, for each step of a request
EARLY = 300.0  # seconds before an access token runs out that another is made
MOST = 1024 * 1024  # bytes of a token answer read, at most

_USER_AGENT = "Stickle"

# What a cloud answers a refresh it turns down (RFC 6749, 5.2, and Microsoft's own).
_SIGN_IN_AGAIN = frozenset(
    {"invalid_grant", "interaction_required", "consent_required", "login_required"}
)
_APP_TURNED_AWAY = frozenset({"invalid_client", "unauthorized_client"})


@dataclass(frozen=True)
class TokenAddress:
    """Where a cloud makes access tokens."""

    host: str
    path: str
    port: int | None = None  # tests only: another than https's own


def waiting(asked: str | None, date: str | None) -> float | None:
    """Seconds a Retry-After asks: a number of them, or a date."""
    if asked is None:
        return None
    try:
        return max(float(int(asked)), 0.0)
    except ValueError:
        pass
    until, now = date_millis(asked), date_millis(date)
    return None if until is None or now is None else max((until - now) / 1000, 0.0)


def error_code(data: bytes) -> str | None:
    """The error a token address answered, by its code alone."""
    try:
        answer = json.loads(data)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    code = cast(dict[str, object], answer).get("error")
    return code if isinstance(code, str) else None


class Tokens:
    """A sign-in's tokens, used from one thread at a time."""

    def __init__(
        self,
        address: TokenAddress,
        client_id: str,
        refresh_token: str,
        renewed: Callable[[str], None],
        *,
        scope: str | None = None,
        public: Callable[[], ssl.SSLContext] = https_context,
        timeout: float = TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """renewed: told a new refresh token as soon as the cloud gives one.
        scope: asked for again with each refresh, as some clouds want."""
        self._address = address
        self._client_id = client_id
        self._refresh = refresh_token
        self._renewed = renewed
        self._scope = scope
        self._public = public
        self._timeout = timeout
        self._clock = clock
        self._context: ssl.SSLContext | None = None
        self._access: str | None = None
        self._until = 0.0  # by clock: when the access token is to be made again

    def __repr__(self) -> str:
        return "Tokens()"

    def access(self) -> str:
        """An access token that has a while to go yet."""
        if self._access is None or self._clock() >= self._until:
            return self.renew()
        return self._access

    def renew(self) -> str:
        """Another access token, as when the cloud turned the last one down."""
        self._access = None
        form = {
            "client_id": self._client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
        }
        if self._scope is not None:
            form["scope"] = self._scope
        status, data, wait = self._post(urlencode(form).encode("ascii"))
        if status != 200:
            raise self._error(status, data, wait)
        try:
            answer = cast(dict[str, object], json.loads(data))
            access, lasts = answer["access_token"], answer["expires_in"]
            refresh = answer.get("refresh_token")
        except ValueError, TypeError, KeyError:
            raise UnavailableError(answered=True) from None
        if (
            not isinstance(access, str)
            or not access
            or isinstance(lasts, bool)
            or not isinstance(lasts, int | float)
        ):
            raise UnavailableError(answered=True)
        if isinstance(refresh, str) and refresh and refresh != self._refresh:
            self._refresh = refresh
            self._renewed(refresh)
        self._access = access
        self._until = self._clock() + max(float(lasts) - EARLY, 0.0)
        return access

    @staticmethod
    def _error(status: int, data: bytes, wait: float | None) -> TargetError:
        if status in (400, 401):
            code = error_code(data)
            if code in _SIGN_IN_AGAIN:
                return SignInNeededError()
            if code in _APP_TURNED_AWAY:
                return NoAccessError()
        if status == 429 or (status == 503 and wait is not None):
            return TooBusyError(wait)
        return UnavailableError(answered=True)

    def _post(self, body: bytes) -> tuple[int, bytes, float | None]:
        if self._context is None:
            self._context = self._public()
        return post_form(self._address, body, self._context, self._timeout)


def post_form(
    address: TokenAddress, body: bytes, context: ssl.SSLContext, timeout: float
) -> tuple[int, bytes, float | None]:
    """A form sent to a token address: its status, what it said, and how many
    seconds it asked to wait (Retry-After). No redirect is followed."""
    connection = http.client.HTTPSConnection(
        address.host, address.port, timeout=timeout, context=context
    )
    try:
        connection.request(
            "POST",
            address.path,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        response = connection.getresponse()
        data = response.read(MOST + 1)
        if len(data) > MOST:
            raise UnavailableError(answered=True)
        wait = waiting(response.getheader("Retry-After"), response.getheader("Date"))
        return response.status, data, wait
    except OSError, http.client.HTTPException:  # ssl's errors among them
        raise UnavailableError() from None
    finally:
        connection.close()
