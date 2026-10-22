# Runbook: bootstrap the SonarCloud project

Reader: a repository administrator setting up or repairing the scan.

Automatic Analysis and the CI scan are mutually exclusive: while Automatic
Analysis is on, SonarCloud refuses the report the gate's `sonar` job uploads.
The one-off bootstrap workflow creates the project and attempts to switch
Automatic Analysis off. Running it again is harmless; it converges on the same
state.

## Steps

```sh
gh workflow run sonar-bootstrap.yml --ref main
```

## Check

The gate's `sonar` job uploads its report without refusal
([CI and branch protection](../reference/ci-and-branch-protection.md)).
