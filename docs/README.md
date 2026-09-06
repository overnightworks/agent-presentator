# Documentation map

Audience: a human or an agent who has just opened `docs/` and needs to know
which layer answers which question.

This file is a map. It owns no product fact. Each layer has its own owner; where
this map and an owner disagree, the owner is right.

## Which question lives where

| Question | Layer | Owner |
| --- | --- | --- |
| Why does this tool exist, and what does the operator want from it? | Vision | [`VISION.md`](VISION.md) |
| What exists today? | Product | [`PRODUCT.md`](PRODUCT.md) |
| Why was it built this way? | Decisions | Records indexed by [`decisions/README.md`](decisions/README.md) |
| What must it be able to do? | Requirements | No owner yet. `docs/requirements/` is created with the first ruled expectation list. |
| How is this installation started and redeployed? | Operations | No owner yet. `docs/OPERATIONS.md` is created with the deploy shell. |

Agent policy lives in [`AGENTS.md`](../AGENTS.md) at the repository root, not
here. The human entry point is [`README.md`](../README.md).

## What this map does not do

It does not list every file, restate a vision sentence, or repeat a decision. It
does not treat a planned layer as present.
