# Lobby settings and appearance reference

Audience: an engineer or operator looking up what the lobby's header,
Settings and Account do and how the lobby is themed.

## Header

Every signed-in page carries the picture's header
([mockups](../mockups/README.md)): the product name, the Decks section,
Settings for an admin, and one person control that opens Account, the three
theme rows, and Log out.

## Settings

An admin opens Settings and sets the instance name, the default language and
the default theme; a person without the admin role is refused there rather
than sent to the login. Settings is one area with a tab strip. There is no
Users tab.

- General holds those instance defaults. The instance name is kept and shown
  back there and read nowhere else.
- Sources lists each source with the state and age of its newest run
  ([sources](sources.md)). An instance with no source says what a source is
  and what you need for it.
- Voice is the admin's voice catalogue
  ([co-presenter and voice](co-presenter-and-voice.md)).

## Account

Everybody opens Account and overrides language and theme for themselves alone.
Both resolve the same way: the person's own choice first, the instance default
behind it. "Follow system" writes no `data-theme` attribute at all, so the
browser decides ([ADR 0012](../decisions/0012-themes-and-language.md)). The
instance defaults live in one SQLite row, a person's overrides in a row beside
their account, and a theme chosen in the person menu is that same Account
preference. Account carries neither a password nor a sessions part.

## Language

English is the only catalog the repository ships. Every catalog file the
catalog directory holds is offered as a language, so a second language is a
file and not a change to code, and each catalog names itself.

## Look

The lobby's colours come from the vendored
[`overnightworks/ui-console`](https://github.com/overnightworks/ui-console)
v0.2.0 pin; Pico leftover chrome lives in `lobby.css`. Settings and Account use
this product's Pico markup, not the package grammar.

htmx 2.x is vendored beside Pico and carries the two writes a person makes
about themselves: the theme rows of the person menu and the Account
preferences post, and the page is painted again in what they chose.
jinja2-fragments ([ADR 0008](../decisions/0008-lobby-server-rendered.md)) is
not installed, because no surface here has a partial to serve: both writes
change the whole document, down to the `<html>` element the theme sits on.
