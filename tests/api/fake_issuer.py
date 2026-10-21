"""An issuer that exists only in a test: a key it generated, tokens it signed.

The relying party reaches it through the two transports the library lets a host
inject, so no test ever contacts a real issuer.
"""

import base64
import json
import secrets
from collections.abc import Mapping
from datetime import datetime
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from webauth.https_client import KeySetFetchError
from webauth.oidc import TokenEndpointAnswer, TokenEndpointError, TokenRequest

A_KEY_ID = "the-key-only-this-test-has"
_TOKEN_LIFETIME_SECONDS = 300


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _base64url_json(document: Mapping[str, object]) -> str:
    return _base64url(json.dumps(document).encode())


def _base64url_integer(number: int) -> str:
    return _base64url(number.to_bytes((number.bit_length() + 7) // 8, "big"))


class FakeIssuer:
    """Signs ID tokens with a generated key and answers the two transports."""

    def __init__(self, *, issuer: str, client_id: str, now: datetime) -> None:
        """Generate the key this issuer signs with, for tokens issued at `now`."""
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._issuer = issuer
        self._client_id = client_id
        self._now = now
        self._tokens_by_code: dict[str, str] = {}
        self.refresh_token: str | None = None
        self.token_endpoint_is_down = False
        self.key_set_is_down = False

    def issue_refresh_token(self, token: str) -> None:
        """Answer every later code exchange with this refresh token too."""
        self.refresh_token = token

    def mint(
        self,
        *,
        nonce: str,
        subject: str,
        groups: list[str],
        username: str | None,
    ) -> str:
        """An ID token for this client."""
        issued_at = int(self._now.timestamp())
        claims: dict[str, object] = {
            "iss": self._issuer,
            "aud": self._client_id,
            "sub": subject,
            "iat": issued_at,
            "exp": issued_at + _TOKEN_LIFETIME_SECONDS,
            "nonce": nonce,
            "groups": groups,
        }
        if username is not None:
            claims["preferred_username"] = username
        header = {"alg": "RS256", "typ": "JWT", "kid": A_KEY_ID}
        signing_input = f"{_base64url_json(header)}.{_base64url_json(claims)}"
        signature = self._key.sign(
            signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_base64url(signature)}"

    def authorize(
        self,
        authorization_url: str,
        *,
        subject: str,
        groups: list[str],
        username: str | None,
    ) -> str:
        """The query the issuer sends the browser back with, after a person signs in."""
        parameters = parse_qs(urlsplit(authorization_url).query)
        code = secrets.token_urlsafe(16)
        self._tokens_by_code[code] = self.mint(
            nonce=parameters["nonce"][0],
            subject=subject,
            groups=groups,
            username=username,
        )
        return f"code={code}&state={parameters['state'][0]}"

    def post_token_request(self, request: TokenRequest) -> TokenEndpointAnswer:
        """The token endpoint: the token minted for the code, or no answer at all."""
        if self.token_endpoint_is_down:
            message = "the token endpoint of a test issuer is down"
            raise TokenEndpointError(message)
        document = {"id_token": self._tokens_by_code[request.form["code"]]}
        if self.refresh_token is not None:
            document["refresh_token"] = self.refresh_token
        return TokenEndpointAnswer(HTTPStatus.OK, json.dumps(document).encode())

    def fetch_key_set(self, url: str) -> bytes:
        """The key set endpoint: the one public key, or a failed fetch."""
        del url
        if self.key_set_is_down:
            message = "the key set endpoint of a test issuer is down"
            raise KeySetFetchError(message)
        numbers = self._key.public_key().public_numbers()
        key = {
            "kty": "RSA",
            "kid": A_KEY_ID,
            "alg": "RS256",
            "use": "sig",
            "n": _base64url_integer(numbers.n),
            "e": _base64url_integer(numbers.e),
        }
        return json.dumps({"keys": [key]}).encode()
