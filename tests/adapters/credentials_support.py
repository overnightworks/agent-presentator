"""A credential resolver for tests that pull from a local file remote."""

from dataclasses import dataclass

from gitmirror.model import CredentialReference


@dataclass(frozen=True, slots=True)
class OpenCredentials:
    """A stand-in so a local bare remote can be pulled without a stored secret."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return "unused-on-a-file-url"
