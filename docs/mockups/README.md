# Mockups

Audience: anyone ruling or building a surface; the picture owner for the lobby.

This directory holds the picture source for a surface change and the PNGs
rendered from it for review. `m0-lobby.html` is the source of truth — a single
HTML sheet of artboards, one `<figure class="board">` per screen state; the
PNGs in `m0-lobby/` and the overview `m0-lobby-sheet.png` are derived renders
committed alongside it so a reader never has to open the HTML or an external
artifact link to see the picture. Blessed by the operator on 06.09.2026
(issue #8); this sheet is the picture owner for the M0 lobby. A change to a
surface starts here: extend this sheet, get the blessing on the owning item,
re-render, then rule.

The Settings · Voice picture was blessed by the operator on 10.09.2026 (issue
#123, parent #108). Its adopted V1–V9 behavior contract is owned by
[`docs/requirements/0001-voice-models.md`](../requirements/0001-voice-models.md);
the mockup remains future surface evidence, not an implementation claim.

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

### First start (line 14)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/setup.png` | First start | 14 |

### Deck list (lines 1, 3, 4, 8, 9, 18)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/deck-list-empty.png` | First day, empty | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/deck-list.png` | Five decks | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/person-menu.png` | Person menu open | 15, 22 |
| `m0-lobby/deck-list-narrow.png` | Split screen, 390 px | group 1, 3, 4, 8, 9, 18 |

### Settings · General (lines 21, 22, 23)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/settings-general.png` | General | 21, 22, 23 |

### Settings · Sources (lines 1, 4, 4a, 7, 14a)

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
| `m0-lobby/sources-detail-ssh.png` | Source — SSH deploy key, 1024 px | 1, 7, 14a, 4a |
| `m0-lobby/sources-detail-ssh-narrow.png` | Source — SSH deploy key, 390 px | 1, 7, 14a, 4a |

### Settings · Users (lines 14, 14b)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/users.png` | Users | 14b |
| `m0-lobby/users-narrow.png` | Split screen, 390 px | group 14, 14b |
| `m0-lobby/users-create.png` | Create user | 14 |
| `m0-lobby/users-deactivate.png` | Deactivate — confirm | 14b |

### Settings · Voice (picture blessed 10.09.2026, V1–V9 adopted)

| PNG | Caption | Ruling |
|---|---|---|
| `m0-lobby/voice.png` | Voice · resting | [V1–V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-narrow.png` | Split screen, 390 px | [V1–V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-downloading.png` | Downloading | [V1, V3, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-loading.png` | Loading | [V2–V4, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-failed-active.png` | Load failed — previous voice stays active | [V4, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-failed-no-active.png` | Load failed — no active voice | [V4, V5, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-service-unknown.png` | Service status unknown | [V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-sample-loading.png` | Sample: loading | [V8, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-sample-playing.png` | Sample: playing | [V8, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-sample-error.png` | Sample: playback failed | [V8, V9](../requirements/0001-voice-models.md) |

Piper and Chatterbox are installed on the live instance (921c53f2); Qwen3-TTS
0.6B, VoxCPM2, and NVIDIA Magpie are proposed candidates only, not yet
integrated. Their not-downloaded state depicts intended future behaviour, not
a working download. No benchmark, percentage, size, or cost is shown for any
candidate — none is measured yet.

### Account (lines 14b, 21, 22)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/account.png` | Account | 14b, 21, 22 |

### Deck page (lines 7, 9, 10, 16)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/deck-page-ready.png` | Ready | 7 |
| `m0-lobby/deck-page-building.png` | Building | 10 |
| `m0-lobby/deck-page-failed.png` | Failed | 9, 16 |

M0 has no Start: the deck page offers presenter view, projector view, and the
PDF. Start belongs to the co-presenter run in M1. Instance language and theme
live under Settings (admin); a personal override lives on Account, and theme
in the bar lives in the person menu (lines 21, 22, 23).
