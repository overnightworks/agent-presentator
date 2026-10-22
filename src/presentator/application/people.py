"""Erasing a person: every row they own goes, the files of their sources first."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from presentator.application.decks import (
    Decks,
    SourceRemovalRefusal,
    SourceRemovalStale,
)
from presentator.contracts.decks import Source, SourceRemoval
from presentator.contracts.people import (
    KindCount,
    PersonBusy,
    PersonDiskLeftover,
    PersonErased,
    PersonErasure,
    PersonKind,
)
from presentator.ports.decks import DeployKeyDrafts
from presentator.ports.identity import PersonAccounts, PersonSessions
from presentator.ports.preferences import PersonPreferencesStore

REFRESH_LOCK_WAIT: Final = timedelta(seconds=60)
_RUNNING: Final = (KindCount(kind=PersonKind.RUNNING_WORK, count=1),)


@dataclass(frozen=True, slots=True, kw_only=True)
class People:
    """The one use case that takes a person and everything they own away."""

    decks: Decks
    accounts: PersonAccounts
    sessions: PersonSessions
    preferences: PersonPreferencesStore
    key_drafts: DeployKeyDrafts
    lock_wait: timedelta = REFRESH_LOCK_WAIT

    def erase_person(self, subject: str, *, dry_run: bool) -> PersonErasure:
        """Count what the person owns, or remove it, files before rows.

        A person who owns no source never waits for the refresh lock. A dry
        run changes nothing and never waits: a refresh that holds the lock is
        listed as running work. A real run waits up to `lock_wait` for it and
        otherwise answers busy with nothing deleted.
        """
        owned = self._sources_of(subject)
        if dry_run:
            return self._counted(subject, owned)
        if not owned:
            return self._account_gone(subject, self._erased_owned(subject, owned))
        if not self.decks.one_at_a_time.acquire(timeout=self.lock_wait.total_seconds()):
            return PersonBusy(running=_RUNNING)
        try:
            outcome = self._erased_owned(subject, self._sources_of(subject))
        finally:
            self.decks.one_at_a_time.release()
        return self._account_gone(subject, outcome)

    def _counted(self, subject: str, owned: tuple[Source, ...]) -> PersonErased:
        counted = self._census(subject, owned)
        if owned and not self._refresh_is_idle():
            return PersonErased(kinds=(*counted, *_RUNNING))
        return PersonErased(kinds=counted)

    def _refresh_is_idle(self) -> bool:
        lock = self.decks.one_at_a_time
        if not lock.acquire(blocking=False):
            return False
        lock.release()
        return True

    def _erased_owned(
        self, subject: str, owned: tuple[Source, ...]
    ) -> PersonErased | PersonDiskLeftover:
        census = self._census(subject, owned)
        self.sessions.delete_for_user(subject)
        self.preferences.delete(subject)
        self.key_drafts.remove_for_owner(subject)
        for source in owned:
            leftover = self._removed(source)
            if leftover is not None:
                return leftover
        return PersonErased(kinds=census)

    def _account_gone(self, subject: str, outcome: PersonErasure) -> PersonErasure:
        if isinstance(outcome, PersonErased):
            self.accounts.delete_account(subject)
        return outcome

    def _removed(self, source: Source) -> PersonDiskLeftover | None:
        confirmed = self._removal_of(source)
        outcome = self.decks.remove_source(
            source.name,
            expected_deck_count=confirmed.deck_count,
            expected_run_count=confirmed.run_count,
        )
        if outcome is SourceRemovalRefusal.DISK_LEFTOVER:
            return PersonDiskLeftover(source=source.name)
        if outcome is None or isinstance(outcome, SourceRemovalStale):
            message = f"source {source.name} changed under the refresh lock"
            raise RuntimeError(message)
        return None

    def _removal_of(self, source: Source) -> SourceRemoval:
        removal = self.decks.removal(source.name)
        if removal is None:
            message = f"source {source.name} vanished while the person was counted"
            raise RuntimeError(message)
        return removal

    def _sources_of(self, subject: str) -> tuple[Source, ...]:
        return tuple(
            source for source in self.decks.sources.all() if source.owner_id == subject
        )

    def _census(self, subject: str, owned: tuple[Source, ...]) -> tuple[KindCount, ...]:
        removals = [self._removal_of(source) for source in owned]
        carried = [self.decks.store.for_source(source.id) for source in owned]
        counts = {
            PersonKind.ACCOUNT: int(self.accounts.get(subject) is not None),
            PersonKind.SESSIONS: len(self.sessions.sessions_of(subject)),
            PersonKind.PREFERENCES: int(self.preferences.read(subject) is not None),
            PersonKind.KEY_DRAFT: self.key_drafts.count_for_owner(subject),
            PersonKind.SOURCES: len(owned),
            PersonKind.DECKS: sum(len(decks) for decks in carried),
            PersonKind.RUNS: sum(removal.run_count for removal in removals),
            PersonKind.MIRRORS: len(owned),
            PersonKind.BUILDS: sum(
                deck.build is not None for decks in carried for deck in decks
            ),
        }
        return tuple(
            KindCount(kind=kind, count=count) for kind, count in counts.items()
        )
