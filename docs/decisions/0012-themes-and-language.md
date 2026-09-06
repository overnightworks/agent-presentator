# ADR 0012: Appearance is a theme file and language is a catalog; neither is code

Audience: humans and agents writing any user-facing string or any colour.

- Status: ACCEPTED 2026-09-06 — binding from the first line of the lobby, which
  is phase M0 of [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06
- Neighbours: [ADR 0008](0008-lobby-server-rendered.md) owns how the lobby is
  built; [ADR 0005](0005-deck-folder-and-slidev.md) owns the addon this theme
  also reaches

## Context

Both of these are cheap to hold from the first file and expensive to retrofit.
A colour typed into a template and a sentence typed into a route are each a
single line; a thousand of them are a rewrite. The previous version had both
problems by construction — one talk, one language, one look, all inline.

The overlay complicates this. It lives inside the running deck
([ADR 0005](0005-deck-folder-and-slidev.md)), so it appears on top of whatever
the deck looks like — and a deck's look is Slidev's own per-deck theme, which
this product does not own. One instance theme cannot match every deck palette,
so a clash is possible and would be visible on a stage. That is why the gap is
named below rather than papered over with a promise.

## Decision

### Appearance is a theme

The lobby is styled only through design tokens — colour, type, spacing, and
state colours — defined once. A theme is one token file. Switching a theme or
adding one is configuration, not code, and no template names a colour.

The co-presenter overlay in the Slidev addon reads the same token file. Lobby
and overlay share one instance theme, which is what
[#8](https://github.com/overnightworks/agent-presentator/issues/8) line 22
rules: one theme, both surfaces.

Decks themselves are untouched. Slidev's per-deck theme mechanism stays exactly
as it is; this record governs the lobby and the overlay, not slide design.

**Named gap: the overlay does not adapt to a deck's theme.** A deck whose
palette fights the instance theme will look wrong under the overlay. Nothing
here promises otherwise, and the fix — whatever it turns out to be — is owned by
[#8](https://github.com/overnightworks/agent-presentator/issues/8) as a later M1
item, once a real deck has shown what the clash actually looks like.

This record owns appearance. [ADR 0008](0008-lobby-server-rendered.md) chooses
the base stylesheet the lobby ships; the tokens here are what drive it.

The mockup at
[the operator's canvas](https://claude.ai/code/artifact/4a808e26-07d5-49ca-858e-a99677d63e0d)
already uses a token set, and it is the first theme's source.

### Language is an adapter

Every user-facing string in the lobby and in the overlay goes through one
message catalog, from the first line of code. English is the only catalog at
start. Adding a language is adding a catalog file — never a code change.

The instance's UI language is configuration. A per-user preference is deferred,
owned by [#8](https://github.com/overnightworks/agent-presentator/issues/8).

Deck content is never translated. A deck is written in a language, and its
manifest says which ([ADR 0005](0005-deck-folder-and-slidev.md), `language`).
The AI presents a deck in the deck's language regardless of the interface
language around it.

### Candidates to vet

Named as candidates, not as choices; each is vetted before the first string
lands, per the rule that an existing solution is looked for before one is
written. For the Jinja2 lobby: Babel with gettext `.po` catalogs. For the addon
overlay: `vue-i18n`.

## Consequences

- A review rejects any literal user-facing string outside the catalog, and any
  colour outside the token set. That is the whole enforcement, and it is a
  review rule rather than a gate because no cheap check reads intent from a
  string.
- The first slice pays for a catalog and a token file that serve one language
  and one theme. That is the deliberate cost of the two retrofits it avoids.
- The overlay looks like the instance, not like the deck. That is the accepted
  cost of one theme file, and it is the gap named above rather than a defect to
  be found later.
- English-only at start means the operator's own German talks run under an
  English interface until a German catalog is added. Adding it is a file.

## Rejected alternatives

- **Hard-coded strings.** The previous version's approach. Every one of them is
  a future find-and-replace across templates, and none of them is findable by a
  tool that does not already know it is a string for a person.
- **A string table per component.** It removes the literal and keeps the
  scatter: the same sentence ends up in two tables, and neither is the owner.
- **Translating deck content.** A talk is written by a person for a room. A
  translated slide is a different talk, and this product does not own that
  decision.
- **Colours in templates, with a theme extracted later.** The extraction is the
  expensive half. Defining the token set first costs one file.
