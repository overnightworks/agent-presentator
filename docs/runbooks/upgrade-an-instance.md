# Runbook: upgrade an instance

Reader: the operator of a containerised instance.

## Before you start

- [Take a backup](back-up-an-instance.md).
- A database written by an older version is refused at start; for that see
  [start over from an old database](start-over-from-an-old-database.md).
- The host speech service must already serve `/health` and `/hear` on
  `speech.sock`; against an older speech, `/copresenter/who` answers 503 and
  hearing reports its failure.
- `docker compose up -d` needs the external network `hal_agent_presentator`;
  edge creates it, and compose fails without it.

## Steps

```sh
git pull && docker compose --profile images build && docker compose up -d
```

## Check

Sign in and open a deck. On stage, ask the co-presenter one question and see
the answer arrive.

Related: [start an instance](start-an-instance-in-a-container.md).
