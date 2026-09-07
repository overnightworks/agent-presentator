"""What the box writes down, and what it refuses to read back."""

from base64 import urlsafe_b64encode

import pytest
from cryptography.fernet import Fernet, InvalidToken

from presentator.adapters.secrets import secret_box

# Exactly the thirty-two characters an instance key is at least, so the bytes
# below are a usable Fernet key without any padding of their own.
_INSTANCE_KEY = "an instance key of thirty-two ch"
_ANOTHER_INSTANCE_KEY = "the key another instance carries"
# `host.main` hands `HmacSessionCookieSigner` the instance key's own bytes, so
# these are the cookie signer's key material, character for character.
_COOKIE_SIGNERS_MATERIAL = _INSTANCE_KEY.encode()
_WHAT_THE_GIT_HOST_EXPECTS = "the read-only words only this test made up"


def test_a_secret_comes_back_out_of_the_box_it_went_into() -> None:
    box = secret_box(_INSTANCE_KEY)

    stored = box.encrypt(_WHAT_THE_GIT_HOST_EXPECTS)

    assert box.decrypt(stored) == _WHAT_THE_GIT_HOST_EXPECTS


def test_what_the_box_wrote_down_is_not_the_secret() -> None:
    stored = secret_box(_INSTANCE_KEY).encrypt(_WHAT_THE_GIT_HOST_EXPECTS)

    assert _WHAT_THE_GIT_HOST_EXPECTS.encode() not in stored


def test_another_instance_key_is_refused_rather_than_answered() -> None:
    stored = secret_box(_INSTANCE_KEY).encrypt(_WHAT_THE_GIT_HOST_EXPECTS)

    assert secret_box(_ANOTHER_INSTANCE_KEY).decrypt(stored) is None


def test_bytes_somebody_changed_since_are_refused() -> None:
    box = secret_box(_INSTANCE_KEY)
    stored = box.encrypt(_WHAT_THE_GIT_HOST_EXPECTS)

    assert box.decrypt(stored[:-1] + b"x") is None


def test_the_box_does_not_encrypt_with_the_cookie_signers_own_material() -> None:
    box = secret_box(_INSTANCE_KEY)
    signers_box = Fernet(urlsafe_b64encode(_COOKIE_SIGNERS_MATERIAL))

    assert box.decrypt(signers_box.encrypt(_WHAT_THE_GIT_HOST_EXPECTS.encode())) is None
    with pytest.raises(InvalidToken):
        signers_box.decrypt(box.encrypt(_WHAT_THE_GIT_HOST_EXPECTS))
