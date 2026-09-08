"""Settings · Sources: the list, adding one, and the page of one source.

Admin only, refused exactly as Settings · General is. No access secret appears
here. The webhook secret appears on the created screen and after a renewal
exactly once.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Final

from fastapi import APIRouter, Form, Request, Response
from starlette.responses import RedirectResponse
from webauth.proxies import request_is_https

from presentator.api.hooks import hook_address
from presentator.api.pages import Pages
from presentator.api.preferences import SETTINGS
from presentator.application.decks import AddedSource, Decks, SourceRefusal
from presentator.contracts.decks import (
    AccessKind,
    ShownSourceRun,
    SourcePage,
    SourceRemoval,
    SourceRunFailure,
    SourceRunOutcome,
    SourceState,
)
from presentator.contracts.models import User
from presentator.contracts.text import LobbyText

SOURCES: Final = f"{SETTINGS}/sources"
NEW: Final = f"{SOURCES}/new"
PAGE: Final = f"{SOURCES}/{{name}}"
FETCH: Final = f"{SOURCES}/{{name}}/fetch"
ACCESS: Final = f"{SOURCES}/{{name}}/access"
WEBHOOK: Final = f"{SOURCES}/{{name}}/webhook"
REMOVE: Final = f"{SOURCES}/{{name}}/remove"
REMOVE_CONFIRMED: Final = f"{SOURCES}/{{name}}/remove/confirmed"
_HTTPS_ACCESS: Final = "https"
_SSH_ACCESS: Final = "ssh"
_FILE_ACCESS: Final = "file"
_STAY_ON_PAGE: Final = "page"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRow:
    """One source as the list renders it: state, name, URL, access, fetched age."""

    name: str
    url: str
    access: str | None
    state: str
    state_word: str
    fetched: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDraft:
    """What the Add source form shows after a refusal: everything but the secret."""

    name: str
    url: str
    access: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRunRow:
    """One recent poll as the source page renders it."""

    state: str
    state_word: str
    fetched: str
    commit: str | None
    reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDeckRow:
    """One deck chip on the source page."""

    slug: str
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceView:
    """The source page's read model, with words already chosen."""

    name: str
    url: str
    access: str | None
    state: str
    state_word: str
    fetched: str | None
    secret_missing: bool
    address: str
    webhook_secret: str | None
    webhook_secret_held_elsewhere: bool
    # A source on this box carries no secret at all, so its page offers
    # neither the dots nor Renew: there is nothing there to renew.
    carries_no_secret: bool
    runs: tuple[SourceRunRow, ...]
    decks: tuple[SourceDeckRow, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRemovalView:
    """The confirm page's read model: the source's name and one sentence."""

    name: str
    title: str
    body: str


class _ShownOperation(StrEnum):
    """Which mint put a webhook secret in the one-time map."""

    CREATE = "create"
    RENEW = "renew"


@dataclass(frozen=True, slots=True, kw_only=True)
class _ShownOnceKey:
    """A one-time webhook secret, owned by one session and one operation."""

    session_id: str
    name: str
    operation: _ShownOperation


@dataclass(frozen=True, slots=True, kw_only=True)
class _Surfaces:
    """The Sources pages: the list, the form, the created screen, Fetch now."""

    pages: Pages
    decks: Decks
    _shown_once: dict[_ShownOnceKey, str] = field(
        default_factory=dict[_ShownOnceKey, str]
    )
    # A one-time notice of what a removal took with it, shown once on the
    # next visit to the list the same way the webhook secret above is shown
    # once on the next visit to the source's own page; one session ever holds
    # one, so its key is the session alone.
    _removed_once: dict[str, str] = field(default_factory=dict[str, str])

    def sources_page(self, request: Request) -> Response:
        """Show each source and what its newest run said, to an admin."""
        if not _signed_in(request).is_admin:
            return _refused()
        text = self.pages.appearance(request).text
        return self.pages.page(
            request,
            "sources.html",
            sources=self._rows(text),
            removed=self._removed_once.pop(_session_id(request), None),
        )

    def add_page(self, request: Request) -> Response:
        """Offer the form that creates a source, to an admin."""
        if not _signed_in(request).is_admin:
            return _refused()
        return self._form(request)

    def create_source(
        self,
        request: Request,
        name: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
        access: Annotated[str, Form()] = _HTTPS_ACCESS,
        secret: Annotated[str, Form()] = "",
    ) -> Response:
        """Store the source, fetch it once, and show the webhook secret once."""
        person = _signed_in(request)
        if not person.is_admin:
            return _refused()
        added = self.decks.add_source(
            name=name,
            url=url,
            access=access,
            secret=secret,
            owner_id=person.id,
        )
        if not isinstance(added, AddedSource):
            return self._form(
                request,
                draft=SourceDraft(
                    name=name,
                    url=url,
                    access=access or _HTTPS_ACCESS,
                    reason=_refusal_sentence(
                        added,
                        self.pages.appearance(request).text,
                    ),
                ),
            )
        self._shown_once[
            _ShownOnceKey(
                session_id=_session_id(request),
                name=added.source.name,
                operation=_ShownOperation.CREATE,
            )
        ] = added.webhook_secret
        return RedirectResponse(
            f"{SOURCES}/{added.source.name}",
            status_code=HTTPStatus.SEE_OTHER,
        )

    def source_page(self, request: Request, name: str) -> Response:
        """Show the created screen once after add, then the source's own page."""
        if not _signed_in(request).is_admin:
            return _refused()
        shown = self.decks.shown_source(name)
        if shown is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        session_id = _session_id(request)
        created = _ShownOnceKey(
            session_id=session_id,
            name=name,
            operation=_ShownOperation.CREATE,
        )
        secret = self._shown_once.pop(created, None)
        if secret is not None:
            return self.pages.page(
                request,
                "source_created.html",
                source_name=name,
                address=self._hook_url(request, name),
                webhook_secret=secret,
            )
        renewed = _ShownOnceKey(
            session_id=session_id,
            name=name,
            operation=_ShownOperation.RENEW,
        )
        secret = self._shown_once.pop(renewed, None)
        held_elsewhere = _held_elsewhere(self._shown_once, session_id, name)
        text = self.pages.appearance(request).text
        return self.pages.page(
            request,
            "source.html",
            source=self._view(
                request,
                shown,
                text,
                webhook_secret=secret,
                webhook_secret_held_elsewhere=held_elsewhere,
            ),
        )

    def fetch_now(
        self,
        request: Request,
        name: str,
        stay: Annotated[str, Form()] = "",
    ) -> Response:
        """Refresh that one source and return to the list, or to its page."""
        if not _signed_in(request).is_admin:
            return _refused()
        if not self.decks.refresh_named(name):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        destination = f"{SOURCES}/{name}" if stay == _STAY_ON_PAGE else SOURCES
        return RedirectResponse(destination, status_code=HTTPStatus.SEE_OTHER)

    def renew_access(
        self,
        request: Request,
        name: str,
        secret: Annotated[str, Form()] = "",
    ) -> Response:
        """Take a new access secret and show nothing of it back."""
        if not _signed_in(request).is_admin:
            return _refused()
        shown = self.decks.shown_source(name)
        if shown is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        if not self.decks.renew_access(name, secret):
            text = self.pages.appearance(request).text
            return self.pages.page(
                request,
                "source.html",
                source=self._view(request, shown, text, webhook_secret=None),
                refused=text.source_refused_secret,
            )
        return RedirectResponse(f"{SOURCES}/{name}", status_code=HTTPStatus.SEE_OTHER)

    def renew_webhook(self, request: Request, name: str) -> Response:
        """Mint a new webhook secret and show it exactly once on the next GET."""
        if not _signed_in(request).is_admin:
            return _refused()
        minted = self.decks.renew_webhook(name)
        if minted is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        self._shown_once[
            _ShownOnceKey(
                session_id=_session_id(request),
                name=name,
                operation=_ShownOperation.RENEW,
            )
        ] = minted
        return RedirectResponse(f"{SOURCES}/{name}", status_code=HTTPStatus.SEE_OTHER)

    def ask_removal(self, request: Request, name: str) -> Response:
        """Show what removing this source would take with it. Deletes nothing."""
        if not _signed_in(request).is_admin:
            return _refused()
        removal = self.decks.removal(name)
        if removal is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        text = self.pages.appearance(request).text
        return self.pages.page(
            request,
            "source_remove.html",
            removal=_removal_view(removal, text),
        )

    def confirm_removal(self, request: Request, name: str) -> Response:
        """Delete the source and everything only it owned, then say what went."""
        if not _signed_in(request).is_admin:
            return _refused()
        removed = self.decks.remove_source(name)
        if removed is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        text = self.pages.appearance(request).text
        self._removed_once[_session_id(request)] = text.source_removed.format(
            name=removed.name,
            decks=removed.deck_count,
            runs=removed.run_count,
        )
        return RedirectResponse(SOURCES, status_code=HTTPStatus.SEE_OTHER)

    def _form(
        self,
        request: Request,
        draft: SourceDraft | None = None,
    ) -> Response:
        return self.pages.page(
            request,
            "source_new.html",
            draft=draft,
            https_access=_HTTPS_ACCESS,
            ssh_access=_SSH_ACCESS,
            file_access=_FILE_ACCESS,
        )

    def _rows(self, text: LobbyText) -> tuple[SourceRow, ...]:
        return tuple(
            SourceRow(
                name=source.name,
                url=source.url,
                access=_access_word(source.access, text),
                state=source.state.value,
                state_word=_state_word(source.state, text),
                fetched=(
                    None
                    if source.age is None
                    else self.pages.age_in_words(source.age, text.language_tag)
                ),
            )
            for source in self.decks.listed_sources()
        )

    def _view(
        self,
        request: Request,
        shown: SourcePage,
        text: LobbyText,
        *,
        webhook_secret: str | None,
        webhook_secret_held_elsewhere: bool = False,
    ) -> SourceView:
        return SourceView(
            name=shown.name,
            url=shown.url,
            access=_access_word(shown.access, text),
            state=shown.state.value,
            state_word=_state_word(shown.state, text),
            fetched=(
                None
                if shown.age is None
                else self.pages.age_in_words(shown.age, text.language_tag)
            ),
            secret_missing=shown.secret_missing,
            address=self._hook_url(request, shown.name),
            webhook_secret=webhook_secret,
            webhook_secret_held_elsewhere=webhook_secret_held_elsewhere,
            carries_no_secret=shown.access is AccessKind.FILE,
            runs=tuple(self._run_row(run, text) for run in shown.runs),
            decks=tuple(
                SourceDeckRow(slug=deck.slug, title=deck.title) for deck in shown.decks
            ),
        )

    def _run_row(self, run: ShownSourceRun, text: LobbyText) -> SourceRunRow:
        state = _run_state_of(run)
        return SourceRunRow(
            state=state.value,
            state_word=(
                text.source_run_fetched
                if run.outcome is SourceRunOutcome.SUCCESS
                else _state_word(state, text)
            ),
            fetched=self.pages.age_in_words(run.age, text.language_tag),
            commit=run.commit,
            reason=None if run.reason is None else _run_reason(run.reason, text),
        )

    def _hook_url(self, request: Request, name: str) -> str:
        scheme = "https" if request_is_https(request) else request.url.scheme
        return f"{scheme}://{request.url.netloc}{hook_address(name)}"


def source_routes(*, pages: Pages, decks: Decks) -> APIRouter:
    """The Sources addresses, for the lobby factory to include."""
    surfaces = _Surfaces(pages=pages, decks=decks)
    router = APIRouter()
    router.add_api_route(SOURCES, surfaces.sources_page, methods=["GET"])
    router.add_api_route(NEW, surfaces.add_page, methods=["GET"])
    router.add_api_route(NEW, surfaces.create_source, methods=["POST"])
    router.add_api_route(FETCH, surfaces.fetch_now, methods=["POST"])
    router.add_api_route(ACCESS, surfaces.renew_access, methods=["POST"])
    router.add_api_route(WEBHOOK, surfaces.renew_webhook, methods=["POST"])
    router.add_api_route(REMOVE, surfaces.ask_removal, methods=["POST"])
    router.add_api_route(REMOVE_CONFIRMED, surfaces.confirm_removal, methods=["POST"])
    router.add_api_route(PAGE, surfaces.source_page, methods=["GET"])
    return router


def _removal_view(removal: SourceRemoval, text: LobbyText) -> SourceRemovalView:
    """The confirm page's read model, its counts already worded into a sentence."""
    return SourceRemovalView(
        name=removal.name,
        title=text.source_remove_confirm_title.format(name=removal.name),
        body=text.source_remove_confirm_body.format(
            decks=removal.deck_count,
            runs=removal.run_count,
        ),
    )


def _state_word(state: SourceState, text: LobbyText) -> str:
    """The catalog's own word for what a source is, shown with its shape."""
    return {
        SourceState.REACHABLE: text.source_state_reachable,
        SourceState.ERROR: text.source_state_error,
        SourceState.REFUSED: text.source_state_refused,
        SourceState.FAILED: text.source_state_failed,
        SourceState.NEVER_FETCHED: text.source_state_never_fetched,
    }[state]


def _access_word(kind: AccessKind | None, text: LobbyText) -> str | None:
    """The catalog's tag for how this source is read, when the URL names one."""
    if kind is AccessKind.HTTPS:
        return text.source_access_token
    if kind is AccessKind.SSH:
        return text.source_access_deploy_key
    if kind is AccessKind.FILE:
        return text.source_access_local
    return None


def _run_reason(reason: SourceRunFailure, text: LobbyText) -> str:
    """The catalog's sentence for why that poll failed."""
    return {
        SourceRunFailure.UNREACHABLE: text.source_run_unreachable,
        SourceRunFailure.CREDENTIAL_UNRESOLVABLE: text.source_run_secret,
        SourceRunFailure.REFUSED: text.source_run_refused,
        SourceRunFailure.FAILED: text.source_run_failed,
    }[reason]


def _run_state_of(run: ShownSourceRun) -> SourceState:
    """The one word a run row shows, read off its outcome and reason.

    A refused login and a host that answered with something else are each
    worth their own word here too, the same exception the sources list makes
    (`SourceState`'s own docstring).
    """
    if run.outcome is SourceRunOutcome.SUCCESS:
        return SourceState.REACHABLE
    if run.reason is SourceRunFailure.REFUSED:
        return SourceState.REFUSED
    if run.reason is SourceRunFailure.FAILED:
        return SourceState.FAILED
    return SourceState.ERROR


def _refusal_sentence(reason: SourceRefusal, text: LobbyText) -> str:
    """The one sentence the form shows for that refusal."""
    return {
        SourceRefusal.MALFORMED_NAME: text.source_refused_name,
        SourceRefusal.DUPLICATE_NAME: text.source_refused_duplicate_name,
        SourceRefusal.DUPLICATE_URL: text.source_refused_duplicate_url,
        SourceRefusal.USERINFO: text.source_refused_password,
        SourceRefusal.ACCESS_MISMATCH: text.source_refused_access,
        SourceRefusal.BLANK_ACCESS: text.source_refused_secret,
        SourceRefusal.CREDENTIAL_NOT_ALLOWED: text.source_refused_secret_not_allowed,
        SourceRefusal.OUTSIDE_MOUNT: text.source_refused_outside_mount,
    }[reason]


def _signed_in(request: Request) -> User:
    """The guard has already turned away everyone else on these addresses."""
    person: User = request.state.signed_in_person
    return person


def _session_id(request: Request) -> str:
    """The live session the guard stashed, so a secret binds to one session."""
    session_id: str = request.state.signed_in_session_id
    return session_id


def _held_elsewhere(
    shown_once: dict[_ShownOnceKey, str],
    session_id: str,
    name: str,
) -> bool:
    """Whether another session still holds a one-time secret for this source."""
    return any(key.name == name and key.session_id != session_id for key in shown_once)


def _refused() -> Response:
    """Sources is admin only, so a person without the role is turned away."""
    return Response(status_code=HTTPStatus.FORBIDDEN)
