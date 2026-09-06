import { fileURLToPath } from 'node:url'

import { coverageConfigDefaults, defineConfig } from 'vitest/config'

// SonarCloud reads coverage paths from the repository root, so the run is
// rooted there and the report lands where the CI artifact is picked up.
const repositoryRoot = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig({
  root: repositoryRoot,
  test: {
    include: ['frontend/tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcovonly'],
      reportsDirectory: 'reports/frontend-coverage',
      exclude: [...coverageConfigDefaults.exclude, '**/package.json'],
      thresholds: {
        lines: 100,
        statements: 100,
        branches: 100,
        functions: 100,
      },
    },
  },
})
