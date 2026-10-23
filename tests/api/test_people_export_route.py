"""S10 (#293): the export route answers one ZIP and no secret is in it."""

import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import timedelta, timezone
from http import HTTPStatus
from typing import Final, cast

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr

from presentator.application.people import People
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    A_CLIENT_ID,
    AN_ISSUER,
    NOW,
    SigningIn,
    a_browser_at,
    a_lobby_app,
    a_relying_party,
    an_admin_people,
    an_installed_auth,
)
from tests.application.fakes import FakePeople, FakeSourceStore, FrozenClock
from tests.application.person_instance_support import (
    ANOTHER,
    THE_PERSON,
    Instance,
    an_instance,
)

_HAL: Final = "hal-client"
_ADMINS: Final = ["/apps/presentator", "/apps/presentator/admin"]
_PATH: Final = "/api/admin/people/"
_A_HOOK_HASH: Final = b"hook-hash-nobody-may-read"
_A_REFRESH_WORD: Final = "refresh-word-nobody-may-read"
_A_CREDENTIAL_WORD: Final = "credential-nobody-may-read"
_SECRET_WORDS: Final = (
    "hook-hash-nobody-may-read",
    _A_REFRESH_WORD,
    _A_CREDENTIAL_WORD,
    "OPENSSH PRIVATE KEY",
)
_SHARED_KEYS: Final = {
    "people_export",
    "app",
    "subject",
    "exported_at",
    "data",
    "files",
    "withheld",
}


@dataclass(frozen=True, slots=True)
class Scene:
    """A lobby over two people with every kind of row, and hal's token issuer."""

    instance: Instance
    issuer: FakeIssuer
    client: TestClient

    def export(self, subject: str, *, authorized: bool = True) -> Response:
        minted = self.issuer.mint_access_token(
            subject="keycloak-subject-of-the-operator",
            groups=_ADMINS,
            authorized_party=_HAL,
            audience=None,
        )
        headers = {"authorization": f"Bearer {minted}"} if authorized else {}
        return self.client.get(f"{_PATH}{subject}/export", headers=headers)


def a_scene() -> Scene:
    instance = an_instance(
        source_store=FakeSourceStore(
            hashes=dict.fromkeys(("decks", "talks", "notes"), _A_HOOK_HASH),
            secrets=dict.fromkeys(("decks-id", "talks-id"), _A_CREDENTIAL_WORD),
        ),
    )
    instance.sessions.create(
        THE_PERSON.id,
        now=NOW,
        expires_at=NOW + timedelta(hours=1),
        refresh_token=SecretStr(_A_REFRESH_WORD),
    )
    an_offset_instant = (NOW + timedelta(microseconds=123456)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    clock = FrozenClock(instant=an_offset_instant)
    people = FakePeople(users=instance.users, sessions=instance.sessions, clock=clock)
    issuer = FakeIssuer(issuer=AN_ISSUER, client_id=A_CLIENT_ID, now=NOW)
    auth = an_installed_auth(
        clock,
        relying_party=a_relying_party(clock, fetch_key_set=issuer.fetch_key_set),
    )
    app, _ = a_lobby_app(
        people=people,
        signing_in=SigningIn(
            sessions=instance.sessions, auth=auth, decks=instance.decks
        ),
        admin_people=an_admin_people(
            People(
                decks=instance.decks,
                accounts=instance.users,
                sessions=instance.sessions,
                preferences=instance.preferences,
                key_drafts=instance.fakes.key_drafts,
            ),
            clients=frozenset({_HAL}),
            clock=clock,
        ),
    )
    lobby = a_browser_at(
        app, people=people, base_url=A_BROWSERS_ADDRESS, headers=A_BROWSERS_HEADERS
    )
    return Scene(instance=instance, issuer=issuer, client=lobby.client)


def _members(answer: Response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _document(answer: Response) -> dict[str, object]:
    document: dict[str, object] = json.loads(_members(answer)["export.json"])
    return document


def test_the_zip_holds_export_json_alone_in_the_shared_shape() -> None:
    """S10, edge people.md: `export.json` at the root, `files` empty, v1."""
    answer = a_scene().export(THE_PERSON.id)

    assert answer.status_code == HTTPStatus.OK
    assert answer.headers["content-type"] == "application/zip"
    assert answer.headers["cache-control"] == "no-store"
    assert THE_PERSON.id not in answer.headers["content-disposition"]
    assert list(_members(answer)) == ["export.json"]
    document = _document(answer)
    assert set(document) == _SHARED_KEYS
    assert document["people_export"] == 1
    assert document["subject"] == THE_PERSON.id
    assert document["files"] == []


def test_exported_at_is_utc_with_z_and_whole_seconds() -> None:
    """S10, edge people.md: `exported_at` RFC 3339 in UTC with `Z`, whole seconds."""
    document = _document(a_scene().export(THE_PERSON.id))

    assert document["exported_at"] == "2026-01-15T09:00:00Z"


def test_every_kind_of_row_is_exported_and_each_secret_kind_counted_withheld() -> None:
    """S10: every row keyed by the person; each omitted kind in `withheld`."""
    document = _document(a_scene().export(THE_PERSON.id))

    data = cast("dict[str, object]", document["data"])
    assert set(data) == {"account", "sessions", "preferences", "key_draft", "sources"}
    assert data["account"] == {"id": THE_PERSON.id, "username": "pia", "role": "admin"}
    assert data["preferences"] == {"language_tag": "de", "theme": None}
    assert data["key_draft"] == {
        "created_at": "2026-01-15T09:00:00Z",
        "public_key": "ssh-ed25519 AAAAtestkey1 presentator",
    }
    assert (
        data["sessions"]
        == [
            {"created_at": "2026-01-15T09:00:00Z", "expires_at": "2026-01-15T10:00:00Z"}
        ]
        * 2
    )
    sources = cast("list[dict[str, object]]", data["sources"])
    assert [
        (source["name"], source["url"], source["ref"], source["public_key"])
        for source in sources
    ] == [
        ("decks", "git@example.invalid:decks.git", "main", None),
        ("talks", "git@example.invalid:talks.git", "main", None),
    ]
    for source in sources:
        decks = cast("list[dict[str, object]]", source["decks"])
        runs = cast("list[dict[str, object]]", source["runs"])
        assert [deck["slug"] for deck in decks] == [f"{source['name']}-deck"]
        assert [run["outcome"] for run in runs] == ["success"]
        assert [run["at"] for run in runs] == ["2026-01-15T09:00:00Z"]
    assert document["withheld"] == [
        {"kind": "source-credential", "reason": "secret", "count": 2},
        {"kind": "hook-secret-hash", "reason": "secret", "count": 2},
        {"kind": "key-draft-private-key", "reason": "secret", "count": 1},
        {"kind": "refresh-token", "reason": "secret", "count": 1},
    ]


def _every_object_has_sorted_keys(value: object) -> bool:
    if isinstance(value, dict):
        members = cast("dict[str, object]", value)
        return list(members) == sorted(members) and all(
            _every_object_has_sorted_keys(inner) for inner in members.values()
        )
    if isinstance(value, list):
        items = cast("list[object]", value)
        return all(_every_object_has_sorted_keys(inner) for inner in items)
    return True


def test_data_keys_are_sorted_at_every_depth() -> None:
    """D14: `data` is the records as JSON with keys sorted."""
    document = _document(a_scene().export(THE_PERSON.id))

    assert _every_object_has_sorted_keys(document["data"])


def test_no_secret_is_in_any_decompressed_member_or_member_name() -> None:
    """S10: no secret, private key or hook hash, as bytes or text, in any name."""
    members = _members(a_scene().export(THE_PERSON.id))

    for name, content in members.items():
        for word in _SECRET_WORDS:
            assert word not in name
            assert word.encode() not in content
            assert word not in content.decode()


def test_a_second_persons_rows_are_absent() -> None:
    """S10: only rows keyed by the subject."""
    members = _members(a_scene().export(THE_PERSON.id))

    text = members["export.json"].decode()
    assert ANOTHER.id not in text
    assert ANOTHER.username not in text
    assert '"notes"' not in text


def test_an_unknown_subject_answers_empty_and_creates_no_account() -> None:
    """S10 Done when, edge people.md: 200, `data` `{}`, never a `users` row."""
    scene = a_scene()
    accounts_before = dict(scene.instance.users.users)

    answer = scene.export("nobody-at-all")

    assert answer.status_code == HTTPStatus.OK
    document = _document(answer)
    assert document["data"] == {}
    assert document["files"] == []
    assert document["withheld"] == []
    assert scene.instance.users.users == accounts_before


def test_no_token_gets_401_and_no_archive() -> None:
    """S9b's gate covers the route: no bearer, no export."""
    answer = a_scene().export(THE_PERSON.id, authorized=False)

    assert answer.status_code == HTTPStatus.UNAUTHORIZED
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["content-type"] != "application/zip"


def test_an_export_audit_line_names_nobody_and_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D10: one INFO line on the route's logger, counts only."""
    scene = a_scene()
    caplog.set_level(logging.DEBUG)

    scene.export(THE_PERSON.id)

    ours = [r for r in caplog.records if r.name == "presentator.api.people"]
    assert [r.levelno for r in ours] == [logging.INFO]
    assert THE_PERSON.id not in ours[0].getMessage()
