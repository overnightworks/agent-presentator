"""The invariants the identity types own themselves."""

from presentator.contracts.models import Role


def test_an_instance_knows_only_the_admin_and_the_user_role() -> None:
    assert list(Role) == [Role.ADMIN, Role.USER]
