# Mockups

Audience: anyone ruling or building a surface; the picture owner for the lobby.

This directory holds the picture source for a surface change and the PNGs
rendered from it for review. `m0-lobby.html` is the source of truth — a single
HTML sheet of artboards, one `<figure class="board">` per screen state; the
PNGs in `m0-lobby/` and the overview `m0-lobby-sheet.png` are derived renders
committed alongside it so a reader never has to open the HTML or an external
artifact link to see the picture. It is a proposal until the operator's
blessing is recorded on #8 — once ruled, this sentence is replaced with the
blessing date and this file stops changing except for a frozen note.

## M0 lobby sheet

Source: `m0-lobby.html`. Overview: `m0-lobby-sheet.png` (full sheet, 1180 px
wide). Per-artboard renders in `m0-lobby/`, device scale factor 2, cropped to
each `.board` element, in document order. "Lines" gives the issue #8
expectation lines the artboard's own caption cites; where the caption cites
none, the group's line range from the sheet's section header is given instead
(marked "group").

### Login (lines 12, 14)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/login.png` | Resting state | group 12, 14 |
| `m0-lobby/login-error.png` | After failed attempts | group 12, 14 |

### Deck list (lines 1, 3, 4, 8, 9, 18)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/deck-list-empty.png` | First day, empty | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/deck-list.png` | Five decks | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/deck-list-narrow.png` | Split screen, 390 px | group 1, 3, 4, 8, 9, 18 |

### Sources (lines 1, 4, 4a, 7, 14a)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/sources.png` | Three sources, three hosts | 1 |
| `m0-lobby/sources-empty.png` | First day, empty | group 1, 4, 4a, 7, 14a |
| `m0-lobby/sources-narrow.png` | Split screen, 390 px | group 1, 4, 4a, 7, 14a |
| `m0-lobby/sources-add.png` | Add source | 1 |
| `m0-lobby/sources-check-reachable.png` | Check: reachable | group 1, 4, 4a, 7, 14a |
| `m0-lobby/sources-check-auth-failed.png` | Check: auth failed | group 1, 4, 4a, 7, 14a |
| `m0-lobby/sources-check-unreachable.png` | Check: unreachable | 4a |
| `m0-lobby/sources-created.png` | Created — shown once | 1 |
| `m0-lobby/sources-detail.png` | Source | 7, 14a, 4a |

### Accounts (lines 14, 14b)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/setup.png` | First start | 14 |
| `m0-lobby/users.png` | Users | 14b |
| `m0-lobby/users-narrow.png` | Split screen, 390 px | group 14, 14b |
| `m0-lobby/users-create.png` | Create user | 14 |
| `m0-lobby/users-deactivate.png` | Deactivate — confirm | 14b |
| `m0-lobby/account.png` | Account | 14b |

### Deck page (lines 7, 9, 10, 16)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/deck-page-ready.png` | Ready | 7 |
| `m0-lobby/deck-page-building.png` | Building | 10 |
| `m0-lobby/deck-page-failed.png` | Failed | 9, 16 |

M0 has no Start: the deck page offers presenter view, projector view, and the
PDF. Start belongs to the co-presenter run in M1.
