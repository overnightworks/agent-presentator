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

The Keycloak sign-in picture (signed out, No access, Login failed, person menu,
Account; no first-start page, local login form, Users, password change, or
session list) was blessed by the operator on 19.10.2026 (K13, issue #242).

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
(marked "group"). The Voice table's "Ruling" column links the adopted V1–V9
requirements that its caption or rendered state demonstrates.

### Sign-in · Keycloak (K13, #242)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/login-redirect.png` | Not signed in: straight to the central sign-in | K13 |
| `m0-lobby/login.png` | Signed out, 1024 px | K13 |
| `m0-lobby/login-narrow.png` | Signed out, 390 px | K13 |
| `m0-lobby/no-access.png` | No access, 1024 px | K13 |
| `m0-lobby/no-access-narrow.png` | No access, 390 px | K13 |
| `m0-lobby/login-failed.png` | Login failed, 1024 px | K13 |
| `m0-lobby/login-failed-narrow.png` | Login failed, 390 px | K13 |

### Deck list (lines 1, 3, 4, 8, 9, 18)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/deck-list-empty.png` | First day, empty | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/deck-list.png` | Five decks | group 1, 3, 4, 8, 9, 18 |
| `m0-lobby/person-menu.png` | Person menu open | 15, 22, K13 |
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

### Settings · Voice (picture blessed 10.09.2026, V1–V9 adopted)

| PNG | Caption | Ruling |
|---|---|---|
| `m0-lobby/voice.png` | Voice · resting | [V1–V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-narrow.png` | Split screen, 390 px | [V1–V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-downloading.png` | Downloading | [V1, V9](../requirements/0001-voice-models.md) |
| `m0-lobby/voice-loading.png` | Loading | [V2, V3, V9](../requirements/0001-voice-models.md) |
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

### Account (lines 21, 22 · K13)

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/account.png` | Account | 21, 22, K13 |

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

### Presenter Home (proposal · issue #139)

The proposed pictures extend Slidev’s public navigation-controls hook, whose
proposed source owner is `examples/copresenter-deck/custom-nav-controls.vue`.
They do not change the existing Voice picture or claim implementation. A
home-outline Home pill returns to the homepage with all talks; Home is its
accessible name and tooltip. Home and the one CoPresenter instance share the
presenter toolbar layout: its 44 px AI toggle is in flow while inactive, and
its bounded active conversation body grows above that toggle. Home is absent
from projector view.

| PNG | Caption |
|---|---|
| `m0-lobby/presenter-return.png` | Presenter, AI off, Home visible — 1024 px |
| `m0-lobby/presenter-return-narrow.png` | Presenter, focused Home — 390 px |
| `m0-lobby/presenter-home-active.png` | Presenter, AI active — 1024 px |
| `m0-lobby/presenter-home-active-narrow.png` | Presenter, AI active — 390 px |
| `m0-lobby/projector-return-absent.png` | Projector — Home absent, 1024 px |

### Rechtliches (#196)

Impressum and Datenschutz, reachable without a session (the signed-out boards
carry the same links), and the same footer links carried onto a page after
login. The Datenschutz artboards show the
page's layout with shortened text. The words are owned by
`src/presentator/api/legal.py`; [`legal-datenschutz-draft.md`](legal-datenschutz-draft.md)
is their copy for the operator's approval before they go online (issue #196,
line 3).

| PNG | Caption | Lines |
|---|---|---|
| `m0-lobby/legal-deck-list.png` | Deck list with legal links, 1024 px | 4 |
| `m0-lobby/legal-datenschutz.png` | Datenschutz, reachable without login, 1024 px | 3 |
| `m0-lobby/legal-datenschutz-narrow.png` | Datenschutz, reachable without login, 390 px | 3 |
