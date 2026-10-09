"""Rehearsal engine: approved scenarios that produce sealed evidence."""

from .db_migration import DbMigrationRecovery
from .harness import RehearsalReport, RunContext, Scenario, run_rehearsal
from .pipeline_gate import PipelineGateRecovery

# Only these scenarios can be run. Keys match the policy engine's registry.
SCENARIOS: dict[str, type[Scenario]] = {
    cls.requirement.scenario_id: cls for cls in (DbMigrationRecovery, PipelineGateRecovery)
}
