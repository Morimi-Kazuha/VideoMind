"""Private artifact index. Collection never promotes semantic expectations."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dovideo.application.evaluation_contracts import canonical_json
from dovideo.application.evaluation_runner import EvaluationArtifactWriter


def identity(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class BadCaseRegistry:
    def __init__(self, output):
        self.writer = EvaluationArtifactWriter(output)
        self.path = self.writer.directory / "badcases.jsonl"

    def history(self, case_id=None):
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()] if self.path.exists() else []
        return [r for r in rows if case_id is None or r["case_id"] == case_id]

    def collect(self, row, *, artifact, metadata):
        # Only structured execution failures/explicit diagnostic findings.
        # No keyword-based classification of natural language answers.
        findings = list(row.get("findings", []))
        error = row.get("error") or row.get("failureCategory")
        if error:
            category = {"budget_exceeded": "BUDGET_EXCEEDED", "timeout": "BUDGET_EXCEEDED",
                        "provider_failure": "PROVIDER_FAILURE", "evidence_rejected": "EVIDENCE_GUARD_FAILURE",
                        "rewrite_failed": "REWRITE_FAILURE"}.get(error, str(error).upper())
            findings.append(dict(category=category, stage="EXECUTION", expected="deliver a guarded answer or explicit bounded failure", observed=str(error)))
        for finding in findings:
            case_id = row.get("case_id", row.get("caseId"))
            if not case_id:
                raise ValueError("missing case identity")
            key = dict(run=metadata["run_id"], case=case_id, arm=row.get("arm", row.get("strategy")),
                       trial=row.get("trialIndex", 0), category=finding["category"], stage=finding["stage"],
                       finding=identity(finding),
                       input_hash=row.get("input_hash") or identity(row))
            record = dict(badcase_id="bc-" + identity(key)[:24], run_id=metadata["run_id"], case_id=case_id,
                dataset_version=metadata.get("dataset_version", "UNKNOWN"), source_revision=metadata.get("source_revision", "UNKNOWN"),
                failure_category=finding["category"], failure_stage=finding["stage"],
                expected_behavior=finding["expected"], observed_behavior=finding["observed"],
                trace_artifact_path=str(Path(artifact).resolve()), input_hash=key["input_hash"],
                source_code_commit=metadata.get("source_code_commit", "UNKNOWN"),
                model_and_config_digest=metadata.get("model_and_config_digest", "NOT_MEASURED"),
                replay_method=metadata.get("replay_method", "inspect original artifact; semantic replay requires fresh authorization"),
                review_status="NEEDS_REVIEW", regression_test_id=None, resolution_status="OPEN",
                arm=key["arm"], verification_results=[])
            self.writer.ensure_writable()
            if not any(r["badcase_id"] == record["badcase_id"] for r in self.history()):
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.summary()

    def link(self, badcase_id, *, test_id, review_status, verification):
        if review_status != "REVIEWED_CONTRACT":
            raise ValueError("regression promotion requires explicit contract review")
        rows = self.history()
        target = next(r for r in rows if r["badcase_id"] == badcase_id)
        target.update(regression_test_id=test_id, review_status=review_status)
        target["verification_results"].append(verification)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(self.path)
        self.summary()

    def summary(self):
        rows = self.history()
        value = dict(total=len(rows), categories=dict(Counter(r["failure_category"] for r in rows)),
                     linked=sum(r["regression_test_id"] is not None for r in rows),
                     needs_review=sum(r["review_status"] == "NEEDS_REVIEW" for r in rows))
        self.writer.ensure_writable()
        (self.writer.directory / "badcase-summary.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        return value

    def review(self, badcase_id, *, root_cause, evidence_path, resolution_status):
        """Record a structural diagnosis; never promote semantic Gold labels."""
        rows = self.history()
        target = next(r for r in rows if r["badcase_id"] == badcase_id)
        review = dict(root_cause=root_cause, evidence_path=str(Path(evidence_path).resolve()), resolution_status=resolution_status)
        reviews = target.setdefault("root_cause_reviews", [])
        if review not in reviews:
            reviews.append(review)
        target.update(root_cause=root_cause, root_cause_artifact_path=review["evidence_path"],
                      review_status="REVIEWED_CONTRACT", resolution_status=resolution_status)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(self.path)
        self.summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--case")
    args = parser.parse_args()
    registry = BadCaseRegistry(args.registry)
    if args.artifact:
        if not args.metadata:
            parser.error("--artifact requires --metadata")
        text = args.artifact.read_text(encoding="utf-8-sig")
        rows = [json.loads(line) for line in text.splitlines()] if args.artifact.suffix == ".jsonl" else json.loads(text)
        metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig"))
        for row in rows:
            registry.collect(row, artifact=args.artifact, metadata=metadata)
    print(json.dumps(registry.history(args.case), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
