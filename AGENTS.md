Audience: every agent that edits this repository.

Code policy and coordination live once, in the global contract. This file adds
only this repository's facts.

Entry points (pointers only; the facts live there): `docs/README.md` maps the
documentation layers; `docs/VISION.md` owns why this tool exists and what the
operator wants from it; `docs/PRODUCT.md` owns implementation status;
`docs/decisions/README.md` owns technical decision records; `README.md` is the
human entry point to those owners.

## Checks

CI calls the shared quality gate from the `quality` job of
`.github/workflows/ci.yml`, pinned by SHA. Its required check is
`quality / verdict`; `quality/config.toml` lists the jobs that must be green
for it, so a new mandatory CI job is added there by its display name. Secret
scanning and the SonarCloud scan run inside the gate, and
`sonar-project.properties` may carry only the keys the gate allows.

`scripts/check_root_layout.py` is the root allowlist check; a new root entry is
added there.
