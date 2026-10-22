"""The unbound deploy keys an admin mints before a source exists."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from presentator.adapters.decks import create_deck_tables
from presentator.adapters.identity import create_identity_tables
from presentator.adapters.sqlite import rows
from presentator.contracts.decks import DeployKeyDraft
from presentator.contracts.models import Role, User
from tests.adapters.key_draft_support import a_key_drafts_store
from tests.people import seed_user

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_NOTICED_GONE_AT = datetime(2026, 1, 20, 9, tzinfo=UTC)
_OWNER = User(id="the-admin", username="felix", role=Role.ADMIN)


def an_instance_that_was_set_up(tmp_path: Path) -> Path:
    """A database with every table and the admin first start created."""
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    seed_user(database, _OWNER)
    return database


def test_a_minted_draft_is_this_owners_own_and_reused_while_unconsumed(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)

    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    assert minted.owner_id == _OWNER.id
    assert minted.private_key
    assert minted.public_key
    reused = drafts.unconsumed_for(_OWNER.id, newer_than=_PUSHED_AT)
    assert reused == minted


def test_a_draft_older_than_the_asked_moment_is_not_unconsumed(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    drafts.mint(_OWNER.id, at=_PUSHED_AT)

    assert drafts.unconsumed_for(_OWNER.id, newer_than=_NOTICED_GONE_AT) is None


def test_opening_add_concurrently_reuses_one_draft_for_its_owner(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    concurrent_opens = 2
    start_together = Barrier(concurrent_opens + 1)

    def open_add() -> DeployKeyDraft:
        drafts = a_key_drafts_store(database)
        start_together.wait()
        return drafts.get_or_mint(
            _OWNER.id,
            newer_than=_PUSHED_AT - timedelta(days=1),
            at=_PUSHED_AT,
        )

    with ThreadPoolExecutor(max_workers=concurrent_opens) as pool:
        openings = [pool.submit(open_add) for _ in range(concurrent_opens)]
        start_together.wait()
    opened = [opening.result() for opening in openings]

    assert len(opened) == concurrent_opens
    assert {draft.id for draft in opened} == {opened[0].id}
    with rows(database) as cursor:
        count = cursor.execute("SELECT COUNT(*) FROM source_key_drafts").fetchone()[0]
    assert count == 1


def test_binding_a_draft_by_its_owner_consumes_it(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    bound = drafts.bind(minted.id, owner_id=_OWNER.id)

    assert bound == minted
    assert drafts.bind(minted.id, owner_id=_OWNER.id) is None
    assert drafts.unconsumed_for(_OWNER.id, newer_than=_PUSHED_AT) is None


def test_binding_a_draft_another_owner_minted_is_refused(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    minted = drafts.mint("another-admin-entirely", at=_PUSHED_AT)

    assert drafts.bind(minted.id, owner_id=_OWNER.id) is None
    # Refused, not consumed: its own owner can still bind it.
    assert drafts.bind(minted.id, owner_id="another-admin-entirely") == minted


def test_sweeping_deletes_only_drafts_older_than_the_given_moment(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    stale = drafts.mint(_OWNER.id, at=_PUSHED_AT)
    fresh = drafts.mint("another-admin-entirely", at=_NOTICED_GONE_AT)

    drafts.sweep(older_than=_NOTICED_GONE_AT)

    assert drafts.bind(stale.id, owner_id=_OWNER.id) is None
    assert drafts.bind(fresh.id, owner_id="another-admin-entirely") == fresh
