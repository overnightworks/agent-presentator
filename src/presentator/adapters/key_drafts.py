"""Where an admin's unbound deploy key waits before it becomes a source (ADR 0010)."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from presentator.adapters.secrets import SecretBox, generate_deploy_key
from presentator.adapters.sqlite import rows
from presentator.contracts.decks import DeployKeyDraft
from presentator.ports.identity import IdentifierFactory

_ADD_DRAFT: Final = """
INSERT INTO source_key_drafts
    (id, owner_id, public_key, encrypted_private_key, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(owner_id) DO UPDATE SET
    id = excluded.id,
    public_key = excluded.public_key,
    encrypted_private_key = excluded.encrypted_private_key,
    created_at = excluded.created_at
"""
_DRAFT_BY_OWNER: Final = """
SELECT id, owner_id, public_key, encrypted_private_key, created_at
FROM source_key_drafts WHERE owner_id = ?
ORDER BY created_at DESC LIMIT 1
"""
DRAFT_BY_ID_AND_OWNER: Final = """
SELECT id, owner_id, public_key, encrypted_private_key, created_at
FROM source_key_drafts WHERE id = ? AND owner_id = ?
"""
REMOVE_DRAFT: Final = "DELETE FROM source_key_drafts WHERE id = ?"
_SWEEP_DRAFTS: Final = "DELETE FROM source_key_drafts WHERE created_at < ?"
DraftRow = tuple[str, str, str, bytes, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class SqliteSourceKeyDrafts:
    """The `source_key_drafts` table: at most one live keypair per admin."""

    database: Path
    identifiers: IdentifierFactory
    box: SecretBox

    def unconsumed_for(
        self,
        owner_id: str,
        *,
        newer_than: datetime,
    ) -> DeployKeyDraft | None:
        """This admin's own unbound draft, or nothing while it has none this young."""
        with rows(self.database) as cursor:
            found = cursor.execute(_DRAFT_BY_OWNER, (owner_id,)).fetchone()
        draft = None if found is None else draft_of_row(found, box=self.box)
        return draft if draft is not None and draft.created_at >= newer_than else None

    def mint(self, owner_id: str, *, at: datetime) -> DeployKeyDraft:
        """Generate a fresh keypair and store it as this admin's own new draft."""
        pair = generate_deploy_key()
        identifier = self.identifiers.new_id()
        with rows(self.database) as cursor:
            cursor.execute(
                _ADD_DRAFT,
                (
                    identifier,
                    owner_id,
                    pair.public_key,
                    self.box.encrypt(pair.private_key),
                    at.isoformat(),
                ),
            )
        return DeployKeyDraft(
            id=identifier,
            owner_id=owner_id,
            public_key=pair.public_key,
            private_key=pair.private_key,
            created_at=at,
        )

    def get_or_mint(
        self,
        owner_id: str,
        *,
        newer_than: datetime,
        at: datetime,
    ) -> DeployKeyDraft:
        """Reuse one young draft or mint one while holding its owner's SQLite row."""
        with rows(self.database) as cursor:
            cursor.execute("BEGIN IMMEDIATE")
            found = cursor.execute(_DRAFT_BY_OWNER, (owner_id,)).fetchone()
            draft = None if found is None else draft_of_row(found, box=self.box)
            if draft is not None and draft.created_at >= newer_than:
                cursor.execute("COMMIT")
                return draft
            pair = generate_deploy_key()
            identifier = self.identifiers.new_id()
            cursor.execute(
                _ADD_DRAFT,
                (
                    identifier,
                    owner_id,
                    pair.public_key,
                    self.box.encrypt(pair.private_key),
                    at.isoformat(),
                ),
            )
            cursor.execute("COMMIT")
        return DeployKeyDraft(
            id=identifier,
            owner_id=owner_id,
            public_key=pair.public_key,
            private_key=pair.private_key,
            created_at=at,
        )

    def bind(self, draft_id: str, *, owner_id: str) -> DeployKeyDraft | None:
        """Take that draft for a new source, only when this admin owns it."""
        with rows(self.database) as cursor:
            found = cursor.execute(
                DRAFT_BY_ID_AND_OWNER,
                (draft_id, owner_id),
            ).fetchone()
            if found is None:
                return None
            cursor.execute(REMOVE_DRAFT, (draft_id,))
        return draft_of_row(found, box=self.box)

    def sweep(self, *, older_than: datetime) -> None:
        """Delete every draft minted before that moment, private key and all."""
        with rows(self.database) as cursor:
            cursor.execute(_SWEEP_DRAFTS, (older_than.isoformat(),))


def draft_of_row(row: DraftRow, *, box: SecretBox) -> DeployKeyDraft | None:
    """The draft a row holds, or nothing when the instance key cannot open it."""
    identifier, owner_id, public_key, encrypted_private_key, created_at = row
    private_key = box.decrypt(encrypted_private_key)
    if private_key is None:
        return None
    return DeployKeyDraft(
        id=identifier,
        owner_id=owner_id,
        public_key=public_key,
        private_key=private_key,
        created_at=datetime.fromisoformat(created_at),
    )
