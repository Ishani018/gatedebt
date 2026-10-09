"""Summarise rehearsal reports in a CI job (the pipeline's ``evaluate`` stage).

This is a convenience for humans reading the pipeline: a table in the job log,
``summary.json`` and a JUnit file GitLab renders in its test-report UI. It is
NOT how GateDebt decides trust; the server re-verifies everything with the
GitLab API when evidence is ingested.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from app.services.policy import rehearsal_failures

from . import SCENARIOS
from .harness import RehearsalReport


def summarize(evidence_dir: Path) -> tuple[bool, dict]:
    results = []
    for scenario_id, cls in sorted(SCENARIOS.items()):
        path = evidence_dir / f"{scenario_id}.json"
        entry = {"scenario_id": scenario_id, "report": path.name, "checks": []}
        if not path.exists():
            entry.update(status="missing", problems=["REPORT_MISSING"])
        else:
            try:
                report = RehearsalReport.model_validate_json(path.read_text())
            except (ValidationError, ValueError):
                entry.update(status="failed", problems=["REPORT_MALFORMED"])
            else:
                evidence = report.evidence
                problems = [] if evidence.digest_matches() else ["EVIDENCE_DIGEST_MISMATCH"]
                problems += rehearsal_failures(evidence, cls.checks_required())
                entry.update(
                    status="passed" if not problems else "failed",
                    problems=problems,
                    exception_id=evidence.exception_id,
                    evidence_id=evidence.id,
                    run_id=evidence.run_id,
                    source=evidence.source.value,
                    commit_sha=evidence.commit_sha,
                    pipeline_id=evidence.pipeline_id,
                    job_id=evidence.job_id,
                    project_id=report.ci.project_id if report.ci else None,
                    started_at=evidence.started_at.isoformat(),
                    finished_at=evidence.finished_at.isoformat(),
                    classification=evidence.injected_failure_classification.value,
                    cleanup=evidence.cleanup_status.value,
                    checks=[{"check_id": c.check_id, "outcome": c.outcome.value, "detail": c.detail}
                            for c in evidence.check_results],
                )
        results.append(entry)
    ok = all(r["status"] == "passed" for r in results)
    return ok, {"all_passed": ok, "note": "Self-check only; the GateDebt server re-verifies via the GitLab API.",
                "scenarios": results}


def write_junit(summary: dict, path: Path) -> None:
    suites = ET.Element("testsuites", name="gatedebt-rehearsals")
    for entry in summary["scenarios"]:
        suite = ET.SubElement(suites, "testsuite", name=entry["scenario_id"])
        cases = entry["checks"] or [{"check_id": "report", "outcome": "failed", "detail": ""}]
        for check in cases:
            case = ET.SubElement(suite, "testcase", classname=entry["scenario_id"], name=check["check_id"])
            if check["outcome"] != "passed":
                ET.SubElement(case, "failure", message=check["outcome"]).text = check["detail"]
        for problem in entry["problems"]:
            if not problem.startswith(("CHECK_FAILED", "CHECK_MISSING")):
                case = ET.SubElement(suite, "testcase", classname=entry["scenario_id"], name=f"policy:{problem}")
                ET.SubElement(case, "failure", message=problem)
        suite.set("tests", str(len(suite)))
        suite.set("failures", str(sum(1 for c in suite if c.find("failure") is not None)))
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)


def main(evidence_dir: Path, out_dir: Path) -> int:
    ok, summary = summarize(evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_junit(summary, out_dir / "rehearsals-junit.xml")
    for entry in summary["scenarios"]:
        where = f"pipeline {entry.get('pipeline_id')} job {entry.get('job_id')}" if entry.get("job_id") else ""
        print(f"{entry['status'].upper():8} {entry['scenario_id']:26} {entry.get('exception_id', '-'):10} {where}")
        for problem in entry["problems"]:
            print(f"         - {problem}")
    print("ALL REHEARSALS PASSED" if ok else "REHEARSAL FAILURES - see summary.json")
    return 0 if ok else 1
