## Landing classification

Exactly one classification line, as prose — a line inside a code fence is an
example, never a declaration. Write it here:



A lane that owns an issue names it and closes it:

```text
Work-Item: #123
Closes #123
```

A lane that owns no issue names its kind — `docs` or `fix` — and closes
nothing:

```text
No-Item: docs
```

The `pr-check` gate refuses a body that carries no classification line, more
than one, two different items, or a closing reference naming anything but the
work item.

## What landed

## Verified by

Every command that was run against this branch, with its result.

## Why this slice is not smaller
