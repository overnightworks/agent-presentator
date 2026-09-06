import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcovonly'],
      // SonarCloud reads the lcov file from the repository root, and the CI
      // artifact is uploaded from the same path.
      reportsDirectory: '../reports/frontend-coverage',
    },
  },
})
