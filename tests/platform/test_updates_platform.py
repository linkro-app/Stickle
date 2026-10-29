import ssl
import sys

import pytest

from stickle.platform.tls import https_context
from stickle.platform.updates import updated_by_its_store


def test_flatpak_keeps_stickle_up_to_date_itself() -> None:
    assert updated_by_its_store({"FLATPAK_ID": "co.linkro.stickle"})


@pytest.mark.skipif(sys.platform == "win32", reason="the Store package is Windows only")
def test_elsewhere_stickle_checks_itself() -> None:
    assert not updated_by_its_store({})


def test_certificates_and_names_are_checked() -> None:
    context = https_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.skipif(
    sys.platform not in ("win32", "darwin"), reason="Linux reads its distribution's bundle"
)
def test_windows_and_macos_have_the_system_check_the_certificates() -> None:
    # Python's own check reads only the authorities already in the store, and a
    # newly set up Windows adds the website's only when its own check asks for it.
    if sys.platform == "win32" or sys.platform == "darwin":
        import truststore

        assert isinstance(https_context(), truststore.SSLContext)
