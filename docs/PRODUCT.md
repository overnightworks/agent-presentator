# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in and lets an admin and every person say how the
lobby looks and which language it speaks; nothing else of the product exists:
no deck is listed, built, or presented, and nothing is deployed. No phase of
[VISION.md](VISION.md) is reached. M0 is tracked on
[#8](https://github.com/overnightworks/agent-presentator/issues/8); first start
and login landed as
[#22](https://github.com/overnightworks/agent-presentator/issues/22), and
Settings, Account and the person menu as
[#32](https://github.com/overnightworks/agent-presentator/issues/32).

A decision record is a technical choice, not a claim that its slice exists.

## Sections

This index gains a section per subject once that subject has landed behavior to
report.

### Signing in

The first start of an empty instance creates the admin at `/setup`, and that
page is gone as soon as an account exists; there is no other way to an account
yet. `/login` opens a session that SQLite holds, carried by a signed cookie
that expires after twelve idle hours and slides forward on every request.
Wrong name, wrong password, and too many attempts answer with one sentence.
Logging out is a POST that deletes the session row. Every other address, known
or not, answers a redirect to the login until someone is signed in, and no
answer may be replayed from the browser cache. A form another site submitted is
refused: first start and login are answered without a cookie, so `SameSite`
does not cover them, and until `webauth` brings its `CsrfPolicy`
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md)) the lobby checks
that a submitted form came from this instance.

The whole identity implementation is a bridge until `webauth` is tagged
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md),
[ADR 0011](decisions/0011-instance-users.md)): songmaker
[#835](https://github.com/overnightworks/songmaker/issues/835) brings the
stores and [#833](https://github.com/overnightworks/songmaker/issues/833) the
user management, and it is deleted with them. How an instance is started is
[OPERATIONS.md](OPERATIONS.md).

### Settings, Account, and how the lobby looks

Every signed-in page carries the picture's header: the product name, the Decks
section, Settings for an admin, and one person control that opens Account, the
three theme rows, and Log out.

An admin opens Settings and sets the instance name, the default language, and
the default theme; a person without the admin role is refused there rather than
sent to the login. Everybody opens Account and overrides language and theme for
themselves alone. Both resolve the same way — the person's own choice first,
the instance default behind it — and "follow system" writes no `data-theme`
attribute at all, so the browser decides
([ADR 0012](decisions/0012-themes-and-language.md)). The instance defaults live
in one SQLite row, a person's overrides in a row beside their account, and a
theme chosen in the person menu is that same Account preference.

English is the only catalog the repository ships. Every catalog file the
catalog directory holds is offered as a language, so a second language is a
file and not a change to code, and each catalog names itself. The instance name
is kept and shown back under Settings; nothing else reads it yet.

htmx 2.x is vendored beside Pico and carries the two writes a person makes
about themselves: the theme rows of the person menu and the Account
preferences post, and the page is painted again in what they chose.
jinja2-fragments ([ADR 0008](decisions/0008-lobby-server-rendered.md)) is not
installed, because no surface here has a partial to serve: both writes change
the whole document, down to the `<html>` element the theme sits on.

Settings has no Sources and no Users tab yet, and Account carries neither the
password nor the sessions part of the picture; each arrives with the slice that
fills it.
