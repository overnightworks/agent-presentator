# presentator

Audience: a human opening this repository for the first time.

presentator is a tool for giving a talk. The Slidev deck lives on the
operator's home server; he opens it from any browser and presents. Per talk he
can switch on an AI co-presenter that listens to the room and answers questions
about the shown deck with a spoken voice, through the operator's agent service. Without the AI the talk still runs, and a static build
plus a PDF export survives a dead tunnel. Why it exists is [docs/VISION.md](docs/VISION.md); what the instance does is
listed in [docs/README.md](docs/README.md).

## Fact owners

| Durable fact | Authoritative owner |
| --- | --- |
| Which documentation layer answers which question | [docs/README.md](docs/README.md) |
| How an operator does a task: start, upgrade, back up, wire a source | [docs/README.md](docs/README.md) |
| Why this tool exists and its latency targets | [docs/VISION.md](docs/VISION.md) |
| What the instance does, exactly: settings, sign-in, sources, decks, builds | [docs/README.md](docs/README.md) |
| Technical decisions | Records indexed by [docs/decisions/README.md](docs/decisions/README.md) |
| How a change is proven | [docs/TESTING.md](docs/TESTING.md) |
| Reusable agent policy | [AGENTS.md](AGENTS.md); [CLAUDE.md](CLAUDE.md) only loads it for Claude |
| Local speech process | [speech/README.md](speech/README.md) |

Do not copy an owner's facts into another document.

## Verifying a change

CI on the pull request is the gate; the [workflow](.github/workflows/ci.yml)
defines every check for the root, `frontend/`, `speech/` and the
container image. Locally, run the checks the change touches: `pyproject.toml`
configures the Python ones (`ruff`, `pyright`, `lint-imports`, `vulture`,
`pytest`, plus `scripts/check_root_layout.py` and
`scripts/check_catalog_purity.py`), `frontend/package.json` the frontend ones.
Which layer's test is the proof, and how to target a local run, is
[docs/TESTING.md](docs/TESTING.md); what `main` requires is
[docs/reference/ci-and-branch-protection.md](docs/reference/ci-and-branch-protection.md).
