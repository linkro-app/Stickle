"""Sign a test account in, for the tests that use a real OneDrive.

    STICKLE_ONEDRIVE_CLIENT_ID=<the app's ID> STICKLE_TEST_ONEDRIVE=<a file> \\
        uv run python tests/sync/sign_in_onedrive.py

The browser opens at Microsoft's sign-in page: sign in there with the test
account and allow Stickle. The file is then written anew, whole, with the
app's ID and the refresh token, as tests/sync/real_onedrive.py reads it. Keep
it apart from the code, in no repository. Nothing printed says what is in it,
nor which account signed in.

It signs in as the app does (stickle.sync.targets.onedrive.sign_in), with no
way in of its own.
"""

import ssl
import sys
import webbrowser
from collections.abc import Callable

from real_onedrive import VARIABLE, account_file, keep_account

from stickle.cloud_apps import ONEDRIVE_VARIABLE, onedrive_client_id
from stickle.platform.tls import https_context
from stickle.sync.targets.base import TargetError
from stickle.sync.targets.onedrive import MICROSOFT, Place, sign_in
from stickle.sync.targets.sign_in import Pages, SignInFailedError

WAITING = 600.0  # seconds given to signing in

PAGES = Pages(
    "en",
    "Signed in for Stickle's tests. You can close this tab.",
    "Sign-in didn't finish. Run the sign-in again.",
)


def main(
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    place: Place = MICROSOFT,
    public: Callable[[], ssl.SSLContext] = https_context,
    waiting: float = WAITING,
    say: Callable[[str], None] = print,
) -> int:
    client_id, file = onedrive_client_id(), account_file()
    if client_id is None or file is None:
        say(f"Give {ONEDRIVE_VARIABLE} (the app's ID) and {VARIABLE} (the file to write).")
        return 2
    with sign_in(client_id, PAGES, place=place, public=public) as signing:
        address = signing.start()
        if not open_browser(address):
            say("No browser opened. Open this address in one:")
            say(address)
        say("Sign in with the test account in the browser, and allow Stickle.")
        try:
            signed = signing.wait(waiting)
        except SignInFailedError as failure:
            say("Stickle was not allowed." if failure.declined else "Sign-in didn't finish.")
            return 1
        except TargetError as failure:
            say(f"Sign-in didn't finish: {type(failure).__name__}.")
            return 1
    if signed is None:
        say(f"No sign-in in {waiting:.0f} seconds.")
        return 1
    file.parent.mkdir(parents=True, exist_ok=True)
    keep_account(file, client_id, signed.refresh_token)
    say("Signed in. The file is written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
