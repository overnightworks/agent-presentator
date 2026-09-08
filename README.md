# agent-presentator

Audience: a human opening this repository for the first time.

agent-presentator is a tool for giving a talk. The Slidev deck lives on the
operator's home server; he opens it from any browser and presents. Per talk he
can switch on an AI co-presenter — Claude, Codex, or Grok, through his own
subscription — which speaks the slides, navigates itself, listens to the room,
and answers questions. Without the AI the talk still runs, and a static build
plus a PDF export survives a dead tunnel. Why it exists and where it is going is
[docs/VISION.md](docs/VISION.md); what exists today is
[docs/PRODUCT.md](docs/PRODUCT.md).

## Fact owners

| Durable fact | Authoritative owner |
| --- | --- |
| Which documentation layer answers which question | [docs/README.md](docs/README.md) |
| Why this tool exists, its phases and latency targets | [docs/VISION.md](docs/VISION.md) |
| Implementation status | [docs/PRODUCT.md](docs/PRODUCT.md) |
| Technical decisions | Records indexed by [docs/decisions/README.md](docs/decisions/README.md) |
| How a change is proven | [docs/TESTING.md](docs/TESTING.md) |
| Reusable agent policy | [AGENTS.md](AGENTS.md); [CLAUDE.md](CLAUDE.md) only loads it for Claude |
| Local speech process | [speech/README.md](speech/README.md) |

Do not copy an owner's facts into another document. Layers that have no owner
yet are named in [docs/README.md](docs/README.md).

## Verifying a change

These are the commands CI and the tools run. `pyproject.toml` owns the
configuration of the Python ones, `frontend/package.json` that of the frontend
ones.

```sh
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked pyright
uv run --locked lint-imports
uv run --locked vulture
uv run --locked python scripts/check_root_layout.py
uv run --locked python scripts/check_catalog_purity.py
uv run --locked pytest
```

From `frontend/`:

```sh
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm build:example
```

And the image CI builds, from the repository root:

```sh
docker build .
```

Which layer's test is the proof, and how to target a local run, is
[docs/TESTING.md](docs/TESTING.md). CI on the pull request is the gate, and
`main` takes nothing that has not passed it — how that is enforced is
[docs/OPERATIONS.md](docs/OPERATIONS.md).
