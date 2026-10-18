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
