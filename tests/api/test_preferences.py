"""Settings, Account, and the person menu, driven the way a browser drives them."""

import re
from dataclasses import replace
from http import HTTPStatus

import pytest
from httpx2 import Response

from presentator.api.preferences import ACCOUNT, SETTINGS, THEME
from presentator.contracts.models import Role
from presentator.contracts.preferences import ThemeChoice
from presentator.contracts.text import Catalogs
from tests.api.lobby import (
    CATALOGS,
    ENGLISH,
    NEIGHBOUR,
    USERNAME,
    Lobby,
    a_lobby,
    a_user_store,
    an_account,
)

_GERMAN = replace(
    ENGLISH,
    language_tag="de",
    language_name="Deutsch",
    account_title="Konto",
)
_INSTANCE_NAME = "Heim"
_NO_OVERRIDE = ""
_SELECTED = re.compile(r'value="([^"]+)"\s*selected')


def what_the_selects_show(page: str) -> list[str]:
    """The value every select on the page stands on, in the order they appear."""
    return _SELECTED.findall(page)


def an_instance_of_two_people(*, catalogs: Catalogs = CATALOGS) -> Lobby:
    """An admin and a person without the role, both able to sign in."""
    lobby = a_lobby(
        users=a_user_store(
            an_account(USERNAME, role=Role.ADMIN),
            an_account(NEIGHBOUR),
        ),
        catalogs=catalogs,
    )
    lobby.log_in()
    return lobby


def signed_in_as(lobby: Lobby, username: str) -> None:
    lobby.client.post("/logout")
    lobby.log_in(username=username)


def save_settings(
    lobby: Lobby,
    *,
    name: str = _INSTANCE_NAME,
    language: str = ENGLISH.language_tag,
    theme: ThemeChoice = ThemeChoice.FOLLOW_SYSTEM,
) -> Response:
    return lobby.client.post(
        SETTINGS,
        data={"name": name, "language": language, "theme": theme.value},
    )


def save_account(lobby: Lobby, *, language: str, theme: str) -> Response:
    return lobby.client.post(ACCOUNT, data={"language": language, "theme": theme})


@pytest.fixture
def instance() -> Lobby:
    return an_instance_of_two_people()


def test_an_admin_sets_the_instance_name_the_language_and_the_default_theme(
    instance: Lobby,
) -> None:
    saved = save_settings(instance, theme=ThemeChoice.DARK)

    assert saved.status_code == HTTPStatus.SEE_OTHER
    assert saved.headers["location"] == SETTINGS
    page = instance.client.get(SETTINGS).text
    assert _INSTANCE_NAME in page
    assert what_the_selects_show(page) == [
        ENGLISH.language_tag,
        ThemeChoice.DARK.value,
    ]
    assert 'data-theme="dark"' in page


def test_an_admin_with_a_fresh_unnamed_instance_saves_the_default_theme(
    instance: Lobby,
) -> None:
    saved = save_settings(instance, name="", theme=ThemeChoice.DARK)

    assert saved.status_code == HTTPStatus.SEE_OTHER
    assert saved.headers["location"] == SETTINGS
    page = instance.client.get(SETTINGS).text
    assert 'id="instance-name" name="name" value=""' in page
    assert what_the_selects_show(page) == [
        ENGLISH.language_tag,
        ThemeChoice.DARK.value,
    ]
    assert 'data-theme="dark"' in page


def test_the_instance_default_theme_reaches_a_person_who_chose_nothing(
    instance: Lobby,
) -> None:
    save_settings(instance, theme=ThemeChoice.DARK)

    signed_in_as(instance, NEIGHBOUR)

    assert 'data-theme="dark"' in instance.client.get("/").text


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_person_without_the_admin_role_is_refused_at_settings(
    instance: Lobby,
    method: str,
) -> None:
    signed_in_as(instance, NEIGHBOUR)

    refused = (
        instance.client.get(SETTINGS)
        if method == "get"
        else save_settings(instance, theme=ThemeChoice.DARK)
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    signed_in_as(instance, USERNAME)
    assert "data-theme" not in instance.client.get("/").text


def test_only_an_admin_is_offered_the_settings_section(instance: Lobby) -> None:
    assert ENGLISH.section_settings in instance.client.get("/").text

    signed_in_as(instance, NEIGHBOUR)

    assert ENGLISH.section_settings not in instance.client.get("/").text


def test_the_person_menu_offers_account_the_theme_rows_and_log_out(
    instance: Lobby,
) -> None:
    page = instance.client.get("/").text

    assert ENGLISH.menu_account in page
    assert ENGLISH.menu_theme in page
    assert ENGLISH.theme_follow_system in page
    assert ENGLISH.theme_light in page
    assert ENGLISH.theme_dark in page
    assert ENGLISH.log_out in page


def test_choosing_dark_in_the_person_menu_paints_that_persons_pages(
    instance: Lobby,
) -> None:
    chosen = instance.client.post(THEME, data={"theme": ThemeChoice.DARK.value})

    assert chosen.status_code == HTTPStatus.NO_CONTENT
    assert chosen.headers["HX-Refresh"] == "true"
    assert 'data-theme="dark"' in instance.client.get("/").text
    assert 'data-theme="dark"' in instance.client.get(ACCOUNT).text


def test_a_theme_chosen_in_the_menu_leaves_the_neighbours_pages_alone(
    instance: Lobby,
) -> None:
    instance.client.post(THEME, data={"theme": ThemeChoice.DARK.value})

    signed_in_as(instance, NEIGHBOUR)

    assert "data-theme" not in instance.client.get("/").text


def test_following_the_system_again_writes_no_theme_onto_the_page(
    instance: Lobby,
) -> None:
    instance.client.post(THEME, data={"theme": ThemeChoice.DARK.value})

    instance.client.post(THEME, data={"theme": ThemeChoice.FOLLOW_SYSTEM.value})

    assert "data-theme" not in instance.client.get("/").text


def test_account_shows_who_you_are_and_what_you_chose(instance: Lobby) -> None:
    save_account(instance, language=ENGLISH.language_tag, theme=ThemeChoice.LIGHT.value)

    page = instance.client.get(ACCOUNT).text

    assert USERNAME in page
    assert ENGLISH.role_admin in page
    assert what_the_selects_show(page) == [
        ENGLISH.language_tag,
        ThemeChoice.LIGHT.value,
    ]


def test_a_person_without_the_admin_role_reads_their_role_on_account(
    instance: Lobby,
) -> None:
    signed_in_as(instance, NEIGHBOUR)

    assert ENGLISH.role_user in instance.client.get(ACCOUNT).text


def test_an_account_choice_paints_that_persons_pages_and_nobody_elses(
    instance: Lobby,
) -> None:
    changed = save_account(
        instance,
        language=ENGLISH.language_tag,
        theme=ThemeChoice.DARK.value,
    )

    assert changed.status_code == HTTPStatus.NO_CONTENT
    assert changed.headers["HX-Refresh"] == "true"
    assert 'data-theme="dark"' in instance.client.get("/").text
    signed_in_as(instance, NEIGHBOUR)
    assert "data-theme" not in instance.client.get("/").text


def test_handing_the_theme_back_to_the_instance_follows_it_again(
    instance: Lobby,
) -> None:
    save_account(instance, language=_NO_OVERRIDE, theme=ThemeChoice.DARK.value)

    save_account(instance, language=_NO_OVERRIDE, theme=_NO_OVERRIDE)

    assert "data-theme" not in instance.client.get("/").text


def test_an_account_language_switches_that_persons_words_only() -> None:
    instance = an_instance_of_two_people(
        catalogs=Catalogs(by_tag={ENGLISH.language_tag: ENGLISH, "de": _GERMAN}),
    )

    save_account(instance, language=_GERMAN.language_tag, theme=_NO_OVERRIDE)

    assert _GERMAN.account_title in instance.client.get(ACCOUNT).text
    signed_in_as(instance, NEIGHBOUR)
    assert ENGLISH.account_title in instance.client.get(ACCOUNT).text


@pytest.mark.parametrize("address", [SETTINGS, ACCOUNT])
def test_a_catalog_file_this_instance_has_is_offered_as_a_choice(
    address: str,
) -> None:
    instance = an_instance_of_two_people(
        catalogs=Catalogs(by_tag={ENGLISH.language_tag: ENGLISH, "de": _GERMAN}),
    )

    page = instance.client.get(address).text

    assert _GERMAN.language_name in page
    assert ENGLISH.language_name in page


def test_a_language_the_instance_does_not_speak_is_refused_on_settings(
    instance: Lobby,
) -> None:
    refused = save_settings(instance, language="fr")

    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_language_the_instance_does_not_speak_is_refused_on_account(
    instance: Lobby,
) -> None:
    refused = save_account(instance, language="fr", theme=_NO_OVERRIDE)

    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_the_instance_default_language_switches_the_words_for_everybody() -> None:
    instance = an_instance_of_two_people(
        catalogs=Catalogs(by_tag={ENGLISH.language_tag: ENGLISH, "de": _GERMAN}),
    )

    save_settings(instance, language=_GERMAN.language_tag)

    signed_in_as(instance, NEIGHBOUR)
    assert _GERMAN.account_title in instance.client.get(ACCOUNT).text


def test_the_menu_that_writes_a_theme_carries_the_script_that_posts_it(
    instance: Lobby,
) -> None:
    page = instance.client.get("/").text

    assert '<script src="/static/htmx.min.js" defer></script>' in page
    assert instance.client.get("/static/htmx.min.js").status_code == HTTPStatus.OK
