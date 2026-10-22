# Runbook: upgrade an instance

Reader: the operator of a containerised instance.

## Before you start

- [Take a backup](back-up-an-instance.md).
- A database written by an older version is refused at start; for that see
  [start over from an old database](start-over-from-an-old-database.md).
- If the co-presenter runs on the host, its speech service must already serve
  `/health` and `/hear` on `speech.sock`; against an older speech,
  `/copresenter/who` answers 503 and hearing reports its failure.

## Steps

```sh
git pull && docker compose --profile images build && docker compose up -d
```

Then restart `presentator-copresenter.service` if the co-presenter runs.

## Check

Sign in and open a deck. On stage, ask the co-presenter one question and see
the answer arrive.

Related: [start an instance](start-an-instance-in-a-container.md).
