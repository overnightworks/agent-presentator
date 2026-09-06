# slidev-addon-presentator

The Slidev addon of the co-presenter. For anyone working on the frontend: these
checks run from this directory and are identical to CI.

- `pnpm install --frozen-lockfile` — install the dependencies from the lockfile
- `pnpm lint` — ESLint over Vue and TypeScript files
- `pnpm typecheck` — `vue-tsc --noEmit`
- `pnpm test` — Vitest with coverage to `reports/frontend-coverage/lcov.info`
- `pnpm build:example` — build the example deck into `examples/hello-deck/dist`
