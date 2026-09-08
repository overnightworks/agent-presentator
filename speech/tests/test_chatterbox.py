"""Chatterbox language tags and the perth watermarker fallback."""

import perth
import pytest

from speech.chatterbox import _silence_watermarker, language_id


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("de", "de"),
        ("de-DE", "de"),
        ("en_US", "en"),
        ("EN", "en"),
        ("", "de"),
        ("  fr-FR  ", "fr"),
    ],
)
def test_language_id_is_the_iso_code(language: str, expected: str) -> None:
    assert language_id(language) == expected


def test_watermarker_is_perth_dummy_because_chatterbox_has_no_disable_flag() -> None:
    _silence_watermarker()

    assert perth.PerthImplicitWatermarker is perth.DummyWatermarker
