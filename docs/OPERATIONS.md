# Operations

Audience: the operator who runs this installation.

## SonarCloud

The quality gate `presentator` (90% coverage on new code, and Sonar way's other
conditions) is created and assigned to `overnightworks_agent-presentator` by
the same Sonar bootstrap `workflow_dispatch`. That 90% lives only on the gate;
pytest has no fail-under.
