"""Exporting a person: all they own is shown or counted as withheld, no secret."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from presentator.contracts.models import Role, User
from presentator.contracts.people import (
    PersonData,
    PersonExport,
    PersonKind,
    Withheld,
    WithheldReason,
)
from tests.application.fakes import FakeSourceStore
from tests.application.person_instance_support import an_instance

_THE_PERSON = "the-person"
_ANOTHER = "another-person"
_TWO_OF_EACH = 2
_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_A_HOOK_HASH = b"hook-hash-nobody-may-read"
_A_REFRESH_WORD = "refresh-word-nobody-may-read"
_SECRET_WORDS = ("hook-hash-nobody-may-read", _A_REFRESH_WORD, "OPENSSH PRIVATE KEY")


def _secret(kind: PersonKind, count: int) -> Withheld:
    return Withheld(kind=kind, reason=WithheldReason.SECRET, count=count)


def _exported(subject: str = _THE_PERSON) -> PersonExport:
    instance = an_instance(
        source_store=FakeSourceStore(
            hashes=dict.fromkeys(("decks", "talks"), _A_HOOK_HASH)
        ),
    )
    instance.sessions.create(
        _THE_PERSON,
        now=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        refresh_token=SecretStr(_A_REFRESH_WORD),
    )
    return instance.people.export_person(subject)


def test_every_kind_of_row_is_in_the_data_and_each_secret_is_counted_withheld() -> None:
    """S10 (#293): the export covers every row keyed by the person, secrets withheld."""
    export = _exported()

    data = export.data
    assert data.account is not None
    assert data.account.id == _THE_PERSON
    assert len(data.sessions) == _TWO_OF_EACH
    assert data.preferences is not None
    assert data.key_draft is not None
    assert data.key_draft.public_key is not None
    assert sorted(source.name for source in data.sources) == ["decks", "talks"]
    assert sum(len(source.decks) for source in data.sources) == _TWO_OF_EACH
    assert sum(len(source.runs) for source in data.sources) == _TWO_OF_EACH
    assert export.withheld == (
        _secret(PersonKind.SOURCE_CREDENTIAL, _TWO_OF_EACH),
        _secret(PersonKind.HOOK_SECRET_HASH, _TWO_OF_EACH),
        _secret(PersonKind.KEY_DRAFT_PRIVATE_KEY, 1),
        _secret(PersonKind.REFRESH_TOKEN, 1),
    )


def test_no_secret_value_appears_in_any_returned_field() -> None:
    """S10 (#293): the export holds no secret, private key or hook hash."""
    shown = str(asdict(_exported()))

    assert not [word for word in _SECRET_WORDS if word in shown]


def test_a_second_persons_rows_are_absent() -> None:
    """S10 (#293): the export covers only rows keyed by the named person."""
    shown = str(asdict(_exported()))

    assert _ANOTHER not in shown
    assert "notes" not in shown


def test_an_unknown_subject_answers_empty_and_creates_nothing() -> None:
    """S10 (#293) Done when: an unknown subject answers empty and is never created."""
    instance = an_instance()
    accounts_before = dict(instance.users.users)

    export = instance.people.export_person("nobody-at-all")

    assert export.withheld == ()
    assert export.data.account is None
    assert export.data.sessions == ()
    assert export.data.preferences is None
    assert export.data.key_draft is None
    assert export.data.sources == ()
    assert instance.users.users == accounts_before


def test_an_account_alone_is_exported_as_the_account_alone() -> None:
    """S10 (#293): an in-flight exchange left an empty account; only it is shown."""
    instance = an_instance()
    sourceless = User(id="sourceless", username="sam", role=Role.USER)
    instance.users.users[sourceless.id] = sourceless

    export = instance.people.export_person(sourceless.id)

    assert export.data == PersonData(
        account=sourceless,
        sessions=(),
        preferences=None,
        key_draft=None,
        sources=(),
    )
    assert export.withheld == ()


def test_a_built_deck_names_its_built_commit() -> None:
    """D5 (#419): the data names the person's built commits."""
    decks = [deck for source in _exported().data.sources for deck in source.decks]

    assert [deck.built_commit for deck in decks] == [deck.commit for deck in decks]
    assert all(deck.built_commit for deck in decks)
