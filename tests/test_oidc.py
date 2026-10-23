"""The relying party is built from settings; an instance without them cannot start."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from presentator.host import main
from presentator.host.config import ConfigurationError, load_settings
from presentator.host.oidc import (
    client_configs_of,
    oidc_client_config,
    read_client_secret,
    relying_party,
)
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_CLIENT_CREDENTIAL,
    A_CLIENT_ID,
    A_TALK_ADDRESS,
    AN_ISSUER,
    a_settings,
)

_UNDECODABLE: bytes = b"fake-secret-bytes-\xff\xfe"


def test_the_client_is_derived_from_the_issuer_and_the_public_origin() -> None:
    config = oidc_client_config(
        a_settings(), SecretStr(A_CLIENT_CREDENTIAL), origin=A_BROWSERS_ADDRESS
    )

    realm = f"{AN_ISSUER}/protocol/openid-connect"
    assert config.issuer == AN_ISSUER
    assert config.authorization_endpoint == f"{realm}/auth"
    assert config.token_endpoint == f"{realm}/token"
    assert config.jwks_url == f"{realm}/certs"
    assert config.end_session_endpoint == f"{realm}/logout"
    assert config.client_id == A_CLIENT_ID
    assert config.redirect_uri == "https://testserver/auth/callback"
    assert config.post_logout_redirect_uri == "https://testserver/login"
    assert config.scopes == ("openid", "profile", "groups")


# T3.
def test_the_lobby_and_the_talk_host_sign_in_through_one_client_at_their_own_callbacks(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(f"{A_CLIENT_CREDENTIAL}\n", encoding="utf-8")
    settings = a_settings().model_copy(update={"oidc_client_secret_file": secret_file})

    lobby, talk = client_configs_of(settings)

    assert (talk.client_id, talk.client_secret) == (
        lobby.client_id,
        lobby.client_secret,
    )
    assert lobby.redirect_uri == f"{A_BROWSERS_ADDRESS}/auth/callback"
    assert talk.redirect_uri == f"{A_TALK_ADDRESS}/auth/callback"
    assert talk.post_logout_redirect_uri == lobby.post_logout_redirect_uri
    assert lobby.post_logout_redirect_uri == f"{A_BROWSERS_ADDRESS}/login"


@pytest.mark.parametrize(
    ("content", "secret"),
    [
        ("a-secret\n", "a-secret"),
        ("a-secret", "a-secret"),
        ("a-secret\n\n", "a-secret\n"),
    ],
    ids=["one newline", "none", "only one of two"],
)
def test_the_secret_file_is_read_with_one_trailing_newline_stripped(
    tmp_path: Path,
    content: str,
    secret: str,
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(content, encoding="utf-8")

    assert read_client_secret(secret_file).get_secret_value() == secret


def test_the_relying_party_begins_a_login_at_the_issuer_the_settings_name(
    environment: pytest.MonkeyPatch,
) -> None:
    del environment

    started = relying_party(client_configs_of(load_settings())[0]).begin_login()

    assert started.url.startswith(f"{AN_ISSUER}/protocol/openid-connect/auth?")


@pytest.mark.parametrize(
    ("content", "is_directory"),
    [
        (None, False),
        (None, True),
        (b"\n", False),
        (b"   \n", False),
        (b"\t", False),
        (b"\n\n", False),
        (_UNDECODABLE, False),
    ],
    ids=[
        "missing",
        "a directory",
        "empty",
        "spaces",
        "a tab",
        "blank lines",
        "undecodable",
    ],
)
def test_a_secret_file_nobody_can_use_refuses_naming_only_the_setting(
    tmp_path: Path,
    content: bytes | None,
    *,
    is_directory: bool,
) -> None:
    secret_file = tmp_path / "secret"
    if is_directory:
        secret_file.mkdir()
    elif content is not None:
        secret_file.write_bytes(content)

    with pytest.raises(ConfigurationError) as refused:
        read_client_secret(secret_file)

    assert "oidc_client_secret_file" in str(refused.value)
    assert "fake-secret-bytes" not in str(refused.value)


def test_an_instance_whose_secret_file_is_gone_refuses_to_start(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment.setenv("PRESENTATOR_OIDC_CLIENT_SECRET_FILE", str(tmp_path / "gone"))
    settings = load_settings()

    with pytest.raises(ConfigurationError, match="oidc_client_secret_file"):
        main.build_instance(settings)


@pytest.mark.parametrize(
    ("variable", "value", "setting"),
    [
        (
            "PRESENTATOR_OIDC_ISSUER",
            "http://auth.example.test/realms/test",
            "oidc_issuer",
        ),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}/", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}?realm=x", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}?", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}#", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}?#", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER} x", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", f"{AN_ISSUER}\nx", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", "https://auth.example.test:99999/r", "oidc_issuer"),
        ("PRESENTATOR_OIDC_ISSUER", "https://auth.example.test:bad/r", "oidc_issuer"),
        (
            "PRESENTATOR_OIDC_ISSUER",
            "https://auth.example.test:/realms/t",
            "oidc_issuer",
        ),
        (
            "PRESENTATOR_OIDC_ISSUER",
            "HTTPS://auth.example.test/realms/t",
            "oidc_issuer",
        ),
        (
            "PRESENTATOR_OIDC_ISSUER",
            "https://AUTH.example.test/realms/t",
            "oidc_issuer",
        ),
        ("PRESENTATOR_OIDC_CLIENT_ID", "", "oidc_client_id"),
        (
            "PRESENTATOR_OIDC_CLIENT_SECRET_FILE",
            "relative/secret",
            "oidc_client_secret_file",
        ),
    ],
    ids=[
        "http issuer",
        "trailing slash",
        "query",
        "empty query",
        "empty fragment",
        "empty query and fragment",
        "space",
        "newline",
        "port too large",
        "port not a number",
        "empty port delimiter",
        "upper-case scheme",
        "upper-case host",
        "no client id",
        "relative file",
    ],
)
def test_a_setting_that_is_not_a_sign_in_refuses_to_start_naming_it(
    environment: pytest.MonkeyPatch,
    variable: str,
    value: str,
    setting: str,
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ConfigurationError, match=setting):
        load_settings()


def test_an_explicit_port_stays_part_of_the_issuer_exactly_as_given(
    environment: pytest.MonkeyPatch,
) -> None:
    ported = "https://auth.example.test:8443/realms/test"
    environment.setenv("PRESENTATOR_OIDC_ISSUER", ported)

    assert load_settings().oidc_issuer == ported


def test_a_client_the_library_refuses_refuses_to_start_naming_no_value() -> None:
    settings = a_settings().model_copy(update={"oidc_client_id": ""})
    secret = SecretStr(A_CLIENT_CREDENTIAL)

    with pytest.raises(ConfigurationError, match="client_id") as refused:
        oidc_client_config(settings, secret, origin=A_BROWSERS_ADDRESS)

    assert A_CLIENT_CREDENTIAL not in str(refused.value)


# T1.
@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://presentator-talk.test",
        "https://presentator-talk.test/path",
        A_BROWSERS_ADDRESS,
        f"{A_BROWSERS_ADDRESS}:8443",
    ],
    ids=["missing", "http", "a path", "the lobby's hostname", "the lobby's other port"],
)
def test_the_talk_origin_must_be_another_canonical_https_hostname(
    environment: pytest.MonkeyPatch,
    origin: str | None,
) -> None:
    if origin is None:
        environment.delenv("PRESENTATOR_TALK_ORIGIN")
    else:
        environment.setenv("PRESENTATOR_TALK_ORIGIN", origin)

    with pytest.raises(ConfigurationError, match="talk_origin") as refused:
        load_settings()

    assert origin is None or origin not in str(refused.value)


# T15.
def test_both_hosts_share_one_lock_one_account_store_and_one_audit_sink(
    environment: pytest.MonkeyPatch,
) -> None:
    del environment

    lobby, talk = main.installed_auths(load_settings())

    assert talk.lock is lobby.lock
    assert talk.accounts is lobby.accounts
    assert talk.audit is lobby.audit
    assert talk.config.session_secret != lobby.config.session_secret
    assert talk.relying_party is not lobby.relying_party


@pytest.mark.parametrize(
    "variable",
    [
        "PRESENTATOR_OIDC_ISSUER",
        "PRESENTATOR_OIDC_CLIENT_ID",
        "PRESENTATOR_OIDC_CLIENT_SECRET_FILE",
    ],
)
def test_an_instance_without_a_sign_in_setting_refuses_to_start(
    environment: pytest.MonkeyPatch,
    variable: str,
) -> None:
    environment.delenv(variable)

    with pytest.raises(
        ConfigurationError, match=variable.removeprefix("PRESENTATOR_").lower()
    ):
        load_settings()
