# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in, and nothing else of the product exists: no deck
is listed, built, or presented, and nothing is deployed. No phase of
[VISION.md](VISION.md) is reached. M0 is tracked on
[#8](https://github.com/overnightworks/agent-presentator/issues/8); first start
and login landed as
[#22](https://github.com/overnightworks/agent-presentator/issues/22).

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
Logging out is a POST that deletes the session row. Every other address answers
a redirect to the login until someone is signed in, and no answer may be
replayed from the browser cache.

The whole identity implementation is a bridge until `webauth` is tagged
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md),
[ADR 0011](decisions/0011-instance-users.md)): songmaker
[#835](https://github.com/overnightworks/songmaker/issues/835) brings the
stores and [#833](https://github.com/overnightworks/songmaker/issues/833) the
user management, and it is deleted with them. How an instance is started is
[OPERATIONS.md](OPERATIONS.md).
