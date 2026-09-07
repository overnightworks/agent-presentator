# ADR 0013: One library encrypts a source's secret at rest, and the child process is where the plaintext goes

Audience: humans and agents storing a secret in this product, or handing one to
`git`.

- Status: ACCEPTED 2026-09-07 — the box, the ciphertext column and the resolver
  that reads it exist; nothing writes a secret at a surface yet. The Add form
  that fills the column (12.5) and its webhook secret, the end of the
  environment-configured source (12.6), and the deploy key (12.7) are open on
  [#8](https://github.com/overnightworks/agent-presentator/issues/8)
- Date: 2026-09-07
- Decision authority: the reviewed breakdown of #8 slice 12, recorded on
  [#8](https://github.com/overnightworks/agent-presentator/issues/8)
- Neighbours: [ADR 0010](0010-git-sources-mirror.md) owns what a source is and
  borrows atelier-2's credential vocabulary; [ADR 0006](0006-sqlite-and-files.md)
  owns the file the ciphertext stands in;
  [ADR 0001](0001-enforced-layers.md) confines the library to `adapters`

## Context

A source is a git URL plus one read-only secret ([ADR 0010](0010-git-sources-mirror.md)).
Until now that secret was never in this product's hands: the durable record
held the *name* of an environment variable and the operator put the value in
the process environment. A person adding a source at a page cannot do that, so
the instance has to hold the value itself — "stored encrypted and never shown
again", as the blessed board promises.

Nothing in the standard library encrypts. What is needed is one authenticated
cipher, one key derivation from the instance key this product already requires,
and — one slice later — an OpenSSH private key.

## Decision

### One dependency

PyCA `cryptography` owns encryption here. It gives all three from one vetted
package: `Fernet` (AES-128-CBC with HMAC-SHA256, authenticated, no mode, nonce
or MAC choice left to a caller), `HKDF`, and `Ed25519PrivateKey` with OpenSSH
serialisation for the deploy key. It removes hand-rolled nonce management, a
hand-rolled key schedule, and the OpenSSH private-key wire format.

Fernet is dated beside AES-GCM. It is still the pick, because it removes every
parameter a builder could get wrong; hand-rolling `AESGCM` here would add three
decisions to save nothing.

### One secret, and derived key material per use

`PRESENTATOR_SECRET_KEY` stays the one secret an operator sets. Every use
derives its own key material from it rather than using it raw, so no two uses
share bytes:

- HKDF-SHA256 over the instance key's bytes, length 32, a public constant salt
  (a fixed byte string in the module, not a secret), and
  `info=b"presentator/source-access-secret/v1"` — deliberately different
  material from the session cookie's HMAC, which is given the raw key.
- The Fernet key is `urlsafe_b64encode` of those 32 bytes.
- No TTL on decrypt: a stored secret does not expire.

Those parameters live in `adapters/secrets.py` and nowhere else. An
import-linter contract keeps `cryptography` out of every layer above
`adapters`.

### The row says where its secret stands

A source's row carries the ciphertext in a nullable column beside the
`credential_reference` column, which keeps meaning an environment variable's
name. One resolver answers at the pull, and it branches on **which column the
row carries**, never on what a value looks like: ciphertext is opened with the
box, a named variable is read from the environment, and the stored form wins
where a row carries both. The environment form is what an instance configured
from `PRESENTATOR_SOURCE_*` still runs on; it goes with that configuration,
once a surface exists to re-enter a secret.

Ciphertext another instance key wrote is refused rather than answered with
whatever it decodes to. The pull reads that refusal as a credential it cannot
resolve, records the run as failed, and leaves every deck listed — a source
nobody can read says nothing about what it carries.

### Where the plaintext goes

Ciphertext in SQLite, decrypted in the resolver's frame when a fetch starts,
handed to the child process, gone with the frame and the subprocess. For an
HTTPS token that is the credential helper `gitmirror` already runs: the helper
*shape* is on the command line and the value is in the child's environment
under `GITMIRROR_CREDENTIAL`, with any inherited helper cleared and the global
and system git configuration neutralised. The plaintext reaches no `Source`
field, no template context, no log record and no exception message.

### The two honest limits

- **`ps e` run by the same user shows the child's environment.**
  `/proc/<pid>/environ` is owner-only on Linux, so another user cannot read it,
  but this is not a claim that the value is unreadable on the machine. A
  process boundary is where the secret is handed over, not a vault.
- **A Python `str` cannot be wiped.** The interpreter may keep and copy it; the
  mitigation is scope, not erasure. The plaintext exists in one frame and one
  child process, and nothing keeps a reference beyond them.

### Decided here, built later

A record says what was decided on its date, not what stands. These parameters
belong to this decision, so the slices that build them take them rather than
inventing them:

- **The webhook secret is hashed, not encrypted** (12.5). It is shown once and
  never read back, so nothing needs a reversible form: `secrets.token_urlsafe(32)`,
  stored as its SHA-256 only, compared with `compare_digest`. Only the access
  secret must be handed to `git` on every fetch.
- **A deploy key reaches `git` through a key file that lives for one fetch**
  (12.7). `ssh` reads a private key from a file or an agent and nothing else:
  a directory at 0700, the key written exclusively at 0600, removed in a
  `finally`. `StrictHostKeyChecking=accept-new` with a known-hosts file of this
  instance's own, because `ask` under `BatchMode=yes` would fail every first
  fetch with no surface anywhere for pasting a host key. That is trust on first
  use: a first-contact machine-in-the-middle is possible, and a *changed* host
  key is refused, which is the attack that matters after setup.

## Consequences

- The instance holds secrets at rest, and a stolen database file alone yields
  nothing: whoever reads it also needs `PRESENTATOR_SECRET_KEY`.
- Losing or changing the instance key makes every stored secret unreadable.
  That is a refusal and a failed fetch, never rubbish handed to `git`, and it
  is repaired by entering the secret again once a surface offers that.
- One more dependency to keep current, and it is a compiled one. It is also the
  package the deploy key will come from, so it is one dependency for three
  needs.
- Backing up the database is not enough to restore a working instance; the
  instance key belongs in the same backup discipline.

## Rejected alternatives

- **PyNaCl.** No OpenSSH private-key serialisation, so the deploy key would
  need a hand-written wire format.
- **`ssh-keygen` as a subprocess.** It writes the private key to disk before
  anything can encrypt it, adds a required binary to the image, and adds a
  process boundary for one library call.
- **Encrypting with `PRESENTATOR_SECRET_KEY` directly.** One secret used raw in
  two places couples the cookie signature to the secrets at rest; deriving
  costs one function call and keeps them apart.
- **A second key setting for secrets at rest.** Two required values to set,
  rotate and back up, for what a derivation gives for free.
- **A vault or a KMS.** For one self-hosted instance on one machine it adds an
  external dependency and an availability requirement to the fetch path.
- **`GIT_ASKPASS`.** It needs an executable on disk *and* still passes the
  value through the environment — strictly worse than the helper.
- **A token inside the source URL.** It would put a clear-text secret in the
  database and in every log line that names the URL.
