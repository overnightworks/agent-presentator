# Decks reference

Audience: an engineer or operator looking up how decks are found, listed and
delivered. How they are built is [builds](builds.md).

## What counts as a deck

A folder counts as a deck when it carries both `deck.toml` and `slides.md`. Its
folder name is the slug and therefore its address, so changing `title` in the
manifest changes no link. There is no upload and no editing
([ADR 0005](../decisions/0005-deck-folder-and-slidev.md)). A deck belongs to
the account that owns the source it came from, and every deck names the source
that carried it.

Only a refresh reconciles; a page view never does.

- A folder the source no longer carries leaves the list: the refresh marks its
  deck as removed instead of deleting anything, and a folder pushed again under
  the same name loses that mark and is the deck it was, at the same address
  with the same owner.
- Only that source's decks are reconciled, so what one source stopped carrying
  says nothing about another's.
- The folder name is one address for the whole instance: a folder whose name
  another source already carries is skipped with a line in the log, and the
  source that carried it first keeps it.
- A source that cannot be read is logged and says nothing about what it
  carries, so every deck stays listed. A folder whose manifest cannot be read
  is logged too and counts as a folder without a title, so the deck row stands
  as it was.

## The list

The most recently changed deck stands first. Every row carries the state of
its deck's build in one word (ready, building, failed, or never built), with a
shape and a colour of its own, read off the talk that stands and the build last
attempted beside it, never stored. While the viewer has no deck, the list says so and
names the Git address instead of showing an empty table, as long as one source
of the viewer's own is all there is to name; with no source it points an admin
at Sources. The list holds only the decks of the viewer's own sources.

## A deck's page

`/deck/<folder>` shows the deck's title, its folder, the state of its build,
the address it was mirrored from, the short commit the talk it delivers was
built from, and how long ago that build ran. Until a build has switched
anything over, the commit shown is the one the source last carried under that
folder. Beside the state, the page names the theme set this instance builds
with, read from the toolchain project's own `package.json` and omitted rather
than shown empty while that project cannot be read
([ADR 0014](../decisions/0014-toolchain-owns-build-dependencies.md)).

- While a build runs, the page says so with the commit being built and how long
  it has been running, and the two views are shown locked rather than offered:
  the talk that stands may be replaced at any moment, and only a page load says
  it is done. The PDF of the last good build stays offered beside them.
- Where the last build failed, the page names the commit that was tried, how
  long ago, and what the toolchain said, as text and never as markup, cut to
  the last two thousand characters, and says the last talk that was built still
  opens. A build that left no words at all is explained in the lobby's own
  sentence.
- A deck no build has switched over yet says so and offers no view rather than
  a dead link.
- A deck exists only for the account that owns its source: the list, the
  deck page, the projector and presenter views, and the PDF show it to that
  account alone, and to anyone else it answers like a slug no deck carries,
  the same status, body and headers, never a hint that it exists. Sources and
  the co-presenter keep their admin gate on top of this. Two named exceptions:
  a source page's removal confirm counts every deck of that source, and a
  folder name two sources carry belongs to the source that took it in first.

## Delivery

Where a deck's built talk stands, which file its PDF is handed over as, which
commit both were built from, and when, are four columns on the deck's row that
one statement writes together, so a talk, its PDF and its build time always
belong to the same commit. Taking the deck in from Git again moves none of
them, so a new push never unpresents the talk that already works nor takes
away the PDF that already downloads.

- The projector view is `/deck/<folder>/` and the presenter view
  `/deck/<folder>/presenter/` at the talk host, both the same Slidev
  single-page application: the presenter is a client-side route of that
  application. The slide is in the address so a closed window comes back to
  the same slide. The deck page links both there, absolute to the talk origin,
  and the lobby serves no talk byte: its old talk addresses answer 404,
  `/deck/<folder>/` answers a 307 to the deck page, and a WebSocket into one
  is closed before it is accepted, alike for every slug.
- A path under the deck that is not a real file is answered with the
  application's `index.html`. A projector or presenter address under a deck
  that is unknown, not built, another account's, or whose build directory has
  since gone from disk, answers a bare empty 404 with no templated page and no
  content type; only the deck's own page route on the lobby renders the
  lobby's not-found page.
- The views stand behind the talk host's own session
  ([ADR 0016](../decisions/0016-a-talk-host-signs-in-by-itself.md)), the deck
  page and the PDF behind the lobby's
  ([ADR 0007](../decisions/0007-browser-client-behind-tunnel.md)): a
  signed-out request answers the login redirect, never a file and never a hint
  that a folder exists, and a WebSocket upgrade into a talk is closed before
  anything is read. The address only chooses a row; the directory comes
  from that row, so no part of a request becomes part of a path, and a path
  that would leave the build directory (through `..`, percent-encoded
  separators, or a symlink out of it) is refused.
- The presenter toolbar has Home for the deck overview and an AI switch; Home
  turns the co-presenter off before it leaves, and neither control appears on
  the projector.
- `/deck/<folder>/pdf` hands the file that row names over as
  `application/pdf`, to be saved under the deck's folder name, and the page
  offers that download only while the column is set. An address with nothing
  behind it (no file put yet, or one gone from disk) answers the lobby's own
  not-found page rather than a traceback, and a signed-out request answers
  the login. The saved name is derived from the folder name alone, and a slug
  that is no folder's own name (carrying a separator, a quote, or a line
  break) hands over nothing, so nothing an address carries reaches a response
  header.
