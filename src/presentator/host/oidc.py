"""The relying party this instance signs people in through, built from settings."""

import time
from pathlib import Path
from typing import Final

from pydantic import SecretStr
from webauth.https_client import fetch_key_set_over_https
from webauth.oidc import OidcClientConfig, OidcRelyingParty
from webauth.oidc_login import AdmissionConfig
from webauth.oidc_transport import post_token_request_over_https

from presentator.api.auth import LOGIN_PATH, LOGOUT_PATH, SIGN_IN_CALLBACK_PATH
from presentator.host.config import Settings, cannot_start

_PROTOCOL_PATH: Final = "protocol/openid-connect"
_SCOPES: Final = ("openid", "profile", "groups")
_CLOCK_LEEWAY_SECONDS: Final = 30
# Who may enter is set in Keycloak alone: these two group paths are the
# product's, not a deployment's.
_LOGIN_GROUP: Final = "/apps/presentator"
_ADMIN_GROUP: Final = "/apps/presentator/admin"
# What the refusals say of the secret file; neither quotes the file or the
# operating system's words, which may carry what the file holds.
_UNREADABLE: Final = "oidc_client_secret_file: is a file this instance can read"
_EMPTY: Final = "oidc_client_secret_file: is a file that holds the client secret"


def read_client_secret(secret_file: Path) -> SecretStr:
    """Read the client secret once, minus the one newline an editor leaves.

    The refusals name the setting and never what the file holds or what the
    operating system says of it, which may quote either.
    """
    try:
        content = secret_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise cannot_start(_UNREADABLE) from None
    secret = content.removesuffix("\n")
    if not secret.strip():
        raise cannot_start(_EMPTY)
    return SecretStr(secret)


def oidc_client_config(
    settings: Settings, client_secret: SecretStr
) -> OidcClientConfig:
    """The Keycloak client these settings name, with its endpoints derived."""
    realm = f"{settings.oidc_issuer}/{_PROTOCOL_PATH}"
    try:
        return OidcClientConfig(
            issuer=settings.oidc_issuer,
            authorization_endpoint=f"{realm}/auth",
            token_endpoint=f"{realm}/token",
            jwks_url=f"{realm}/certs",
            end_session_endpoint=f"{realm}/logout",
            client_id=settings.oidc_client_id,
            client_secret=client_secret,
            redirect_uri=f"{settings.public_origin}{SIGN_IN_CALLBACK_PATH}",
            post_logout_redirect_uri=f"{settings.public_origin}{LOGIN_PATH}",
            logout_path=LOGOUT_PATH,
            scopes=_SCOPES,
            leeway_seconds=_CLOCK_LEEWAY_SECONDS,
        )
    except ValueError as refused:
        # webauth's reasons name a field, never its value.
        message = f"the sign-in client configuration: {refused}"
        raise cannot_start(message) from None


def relying_party(config: OidcClientConfig) -> OidcRelyingParty:
    """The relying party over the library's production transports and clocks."""
    return OidcRelyingParty(
        config,
        post_token_request=post_token_request_over_https,
        fetch_key_set=fetch_key_set_over_https,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )


def relying_party_of(settings: Settings) -> OidcRelyingParty:
    """Read the secret file and build the relying party these settings describe."""
    secret = read_client_secret(settings.oidc_client_secret_file)
    return relying_party(oidc_client_config(settings, secret))


def admission_of(settings: Settings) -> AdmissionConfig:
    """Only members of the app's Keycloak group enter, and its admin group admins."""
    return AdmissionConfig(
        bound_issuer=settings.oidc_issuer,
        login_group=_LOGIN_GROUP,
        admin_group=_ADMIN_GROUP,
    )
