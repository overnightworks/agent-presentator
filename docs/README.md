# Documentation map

Audience: a human or an agent who has just opened `docs/` and needs to know
which layer answers which question.

This file is a map. It owns no product fact. Each layer has its own owner; where
this map and an owner disagree, the owner is right.

## Which question lives where

| Question | Layer | Owner |
| --- | --- | --- |
| Why does this tool exist, and what does the operator want from it? | Vision | [`VISION.md`](VISION.md) |
| What does the instance do, exactly? | Reference | the documents under [Reference](#reference) |
| Why was it built this way? | Decisions | Records indexed by [`decisions/README.md`](decisions/README.md) |
| What must it be able to do? | Requirements | [`0001-voice-models.md`](requirements/0001-voice-models.md) |
| How do I run, change or repair this installation? | Runbooks | the documents under [Runbooks](#runbooks) |
| Why are the guards what they are? | Explanation | [`explanation/trust-boundaries.md`](explanation/trust-boundaries.md) |
| How is a change proven? | Testing | [`TESTING.md`](TESTING.md) |
| What does the lobby look like? | Picture | [`mockups/README.md`](mockups/README.md) |

Agent policy lives in [`AGENTS.md`](../AGENTS.md) at the repository root, not
here. The human entry point is [`README.md`](../README.md).

## Runbooks

One task each, in [`runbooks/`](runbooks/).

- Run: [from a checkout](runbooks/run-from-a-checkout.md),
  [in a container](runbooks/start-an-instance-in-a-container.md),
  [install the host build toolchain](runbooks/install-the-host-build-toolchain.md),
  [run builds without a disk bound](runbooks/run-builds-without-a-disk-bound.md).
- Keep: [upgrade](runbooks/upgrade-an-instance.md),
  [back up](runbooks/back-up-an-instance.md),
  [start over from an old database](runbooks/start-over-from-an-old-database.md).
- Sign-in: [configure Keycloak](runbooks/configure-keycloak.md).
- Sources: [add a Git source](runbooks/add-a-source.md),
  [renew a secret](runbooks/renew-a-source-secret.md),
  [add a folder on this box](runbooks/add-a-folder-on-this-box.md),
  [call the fetch-now hook](runbooks/call-the-fetch-now-hook.md).
- Decks: [prepare a talk for the build](runbooks/prepare-a-deck-for-the-build.md).
- Co-presenter: [run it on the host](runbooks/run-the-co-presenter-on-the-host.md),
  [set the Claude token](runbooks/set-the-claude-token.md),
  [clear leftover turn homes](runbooks/clear-leftover-turn-homes.md).
- Repository: [bootstrap SonarCloud](runbooks/bootstrap-sonarcloud.md).

## Reference

In [`reference/`](reference/): [configuration](reference/configuration.md),
[sign-in and session](reference/sign-in-and-session.md),
[lobby settings and appearance](reference/lobby-settings-and-appearance.md),
[sources](reference/sources.md), [decks](reference/decks.md),
[builds](reference/builds.md),
[co-presenter and voice](reference/co-presenter-and-voice.md),
[CI and branch protection](reference/ci-and-branch-protection.md).

## What this map does not do

It lists the runbooks and the reference documents because they are many; it
does not list other files, restate a vision sentence, or repeat a decision. It
does not treat a planned layer as present.
