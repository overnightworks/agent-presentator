"""The invariants the identity types own themselves."""

from presentator.contracts.models import Account, Role, User

_A_STORED_HASH = "a hash"


def test_an_instance_knows_only_the_admin_and_the_user_role() -> None:
    assert list(Role) == [Role.ADMIN, Role.USER]


def test_an_account_without_its_hash_is_the_person() -> None:
    account = Account(
        id="user-1",
        username="felix",
        role=Role.ADMIN,
        password_hash=_A_STORED_HASH,
    )

    assert account.as_user() == User(id="user-1", username="felix", role=Role.ADMIN)
    assert account.is_active is True
