"""Sign a test account in, for the tests that use a real Dropbox.

    STICKLE_DROPBOX_APP_KEY=<the app's key> STICKLE_TEST_DROPBOX=<a file> \\
        uv run python tests/sync/sign_in_dropbox.py

The browser opens at Dropbox's page to allow Stickle: sign in there with the
test account and allow it (should every port the app registered be taken,
Dropbox shows a code instead: paste it here). The file is then written anew,
whole, with the app's key and the refresh token, as tests/sync/real_dropbox.py
reads it. Keep it apart from the code, in no repository. To see the sign-in
works, the app's folder is then listed once. Nothing printed says what is in
the file, nor which account signed in: only whether Dropbox told its name, and
how many things the folder holds.

It signs in as the app does (stickle.sync.targets.dropbox.sign_in), with no
way in of its own.
"""

import ssl
import sys
import webbrowser
from collections.abc import Callable

from real_dropbox import VARIABLE, account_file, keep_account

from stickle.cloud_apps import DROPBOX_VARIABLE, dropbox_app_key
from stickle.platform.tls import https_context
from stickle.sync.targets.base import TargetError
from stickle.sync.targets.dropbox import DROPBOX, App, Place
from stickle.sync.targets.sign_in import Pages, SignedIn, SignIn, SignInFailedError

WAITING = 600.0  # seconds given to signing in

PAGES = Pages(
    "en",
    "Signed in for Stickle's tests. You can close this tab.",
    "Sign-in didn't finish. Run the sign-in again.",
)


def _by_code(
    signing: SignIn, enter_code: Callable[[str], str], say: Callable[[str], None]
) -> SignedIn | None:
    """The code Dropbox showed, entered until one is good, or none is entered."""
    while True:
        code = enter_code("Code Dropbox showed (empty to stop): ").strip()
        if not code:
            return None
        try:
            return signing.enter(code)
        except SignInFailedError:
            say("That code was not taken. Try again.")


def main(
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    enter_code: Callable[[str], str] = input,
    place: Place = DROPBOX,
    public: Callable[[], ssl.SSLContext] = https_context,
    waiting: float = WAITING,
    say: Callable[[str], None] = print,
) -> int:
    app_key, file = dropbox_app_key(), account_file()
    if app_key is None or file is None:
        say(f"Give {DROPBOX_VARIABLE} (the app's key) and {VARIABLE} (the file to write).")
        return 2
    app = App(app_key, place, public)
    with app.sign_in(PAGES) as signing:
        address = signing.start()
        if not open_browser(address):
            say("No browser opened. Open this address in one:")
            say(address)
        say("Sign in with the test account in the browser, and allow Stickle.")
        try:
            signed = (
                _by_code(signing, enter_code, say) if signing.pasting else signing.wait(waiting)
            )
        except SignInFailedError as failure:
            say("Stickle was not allowed." if failure.declined else "Sign-in didn't finish.")
            return 1
        except TargetError as failure:
            say(f"Sign-in didn't finish: {type(failure).__name__}.")
            return 1
    if signed is None:
        say(f"No sign-in in {waiting:.0f} seconds." if not signing.pasting else "No code entered.")
        return 1
    file.parent.mkdir(parents=True, exist_ok=True)
    keep_account(file, app_key, signed.refresh_token)
    say("Signed in. The file is written.")
    say(f"The account's name: {'told' if signed.account else 'not told'}.")
    target = app.target(signed.refresh_token, lambda token: keep_account(file, app_key, token))
    try:
        listed = sum(1 for _ in target.list(""))
    except TargetError as failure:
        say(f"The app's folder could not be listed: {type(failure).__name__}.")
        return 1
    finally:
        target.close()
    say(f"The app's folder holds {listed} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
