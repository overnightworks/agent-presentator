"""The instance deletes expired sessions on a schedule of its own."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

from presentator.adapters.identity import SqliteSessionRecordStore
from presentator.api.legal import DATENSCHUTZ
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host import main
from presentator.host.config import load_settings
from tests.application.fakes import PATIENCE, FrozenClock
from tests.people import seed_user

_STARTED = datetime(2026, 1, 15, 9, tzinfo=UTC)
_ADMIN = User(id="id-of-felix", username="felix", role=Role.ADMIN)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FrozenClock:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRESENTATOR_DATABASE", str(tmp_path / "presentator.sqlite3"))
    monkeypatch.setenv("PRESENTATOR_LOCAL_SOURCES_MOUNT", str(tmp_path))
    monkeypatch.setenv("PRESENTATOR_PUBLIC_ORIGIN", "https://presentator.test")
    monkeypatch.setenv("PRESENTATOR_COPRESENTER_SOCKET", str(tmp_path / "co.sock"))
    monkeypatch.setenv("PRESENTATOR_SECRET_KEY", "an instance key of at least 32 bytes")
    monkeypatch.setenv("PRESENTATOR_BUILD_RUNNER", "host")
    clock = FrozenClock(_STARTED)
    monkeypatch.setattr(main, "SystemClock", lambda: clock)
    return clock


def stored_session_ids(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {row[0] for row in connection.execute("SELECT id FROM sessions")}


def test_the_sweep_reads_the_clock_on_every_run_and_not_once_at_the_start(
    clock: FrozenClock,
) -> None:
    instance = main.build_instance(load_settings())
    database = load_settings().database
    seed_user(database, _ADMIN)
    session = SqliteSessionRecordStore(database).create(
        _ADMIN.id,
        now=clock.now(),
        expires_at=clock.now() + SESSION_LIFETIME,
    )

    asyncio.run(instance.session_sweep.tick())
    assert stored_session_ids(database) == {session.id}

    clock.advance(SESSION_LIFETIME + timedelta(seconds=1))
    asyncio.run(instance.session_sweep.tick())

    assert stored_session_ids(database) == set()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT username FROM users").fetchall() == [
            ("felix",),
        ]


def test_the_sweep_runs_while_the_server_serves(
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "SESSION_SWEEP_INTERVAL", timedelta(0))
    database = load_settings().database
    swept: list[bool] = []

    class ServerThatWaitsForTheSweep:
        def __init__(self, config: uvicorn.Config) -> None:
            self.config = config

        async def serve(self) -> None:
            seed_user(database, _ADMIN)
            SqliteSessionRecordStore(database).create(
                _ADMIN.id,
                now=clock.now() - SESSION_LIFETIME,
                expires_at=clock.now() - timedelta(seconds=1),
            )
            patience = asyncio.get_running_loop().time() + PATIENCE.total_seconds()
            while stored_session_ids(database):
                if asyncio.get_running_loop().time() > patience:
                    break
                await asyncio.sleep(0)
            swept.append(not stored_session_ids(database))

    monkeypatch.setattr(main.uvicorn, "Server", ServerThatWaitsForTheSweep)

    main.main()

    assert swept == [True]


def test_the_datenschutz_names_the_sweep_period_of_five_minutes() -> None:
    sentences = [
        "".join(phrase.words for phrase in paragraph)
        for section in DATENSCHUTZ.sections
        for paragraph in section.paragraphs
    ]

    assert any(
        "abgelaufene Sitzungen löscht der Dienst alle 5 Minuten." in sentence
        for sentence in sentences
    )
