"""Verify captured V1 scores without retrieval, ASR, OCR, or Provider calls."""
import argparse
import json
import statistics
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_v1_pilot import golden, load
from prepare_v1_pilot_media import digest, save, private_output, ROOT
from dovideo.application.evaluation_runner import EvaluationRetrievedEvidence
from dovideo.application.evaluation_contracts import read_case_results
from dovideo.application.evaluation_pilot import score_pilot_retrieval


def replay_optimization(args):
    """Replay real optimization rankings through the existing Pilot scorer."""
    manifest, context, dataset, annotations, validation = golden(args.gold_dir, args.media_dir)
    metadata = load(args.run_dir / 'run-metadata.json')
    if (metadata['datasetDigest'] != dataset.dataset_digest or
        metadata['sourceRevision'] != context.source_revision or
        metadata['mediaSha256'] != manifest['mediaSha256'] or
        metadata['sidecarSha256'] != digest(args.gold_dir / 'annotations.json')):
        raise ValueError('execution/input identity mismatch')
    drift = [name for name, expected in metadata['codeDigests'].items() if digest(ROOT/name) != expected]
    production_drift = [name for name in drift if name.startswith('src/')]
    if production_drift:
        raise ValueError('scoring/production source changed: ' + ', '.join(production_drift))
    by_case = {c.case_id:c for c in annotations.cases}
    checked = []
    summaries = load(args.run_dir / 'rag' / 'summary.json')
    for arm in ('dense','hybrid','reranker'):
        rows = load(args.run_dir / 'rag' / (arm + '-results.json'))
        scores = []
        for row in rows:
            if row['status'] != 'PASS':
                continue
            case = dataset.case(row['caseId'])
            ranks = tuple(EvaluationRetrievedEvidence.model_validate(r) for r in row['ranks'])
            score = json.loads(json.dumps(score_pilot_retrieval(case, by_case[case.case_id], ranks)))
            if score != row['score']:
                raise ValueError('score changed on captured ranks: ' + arm + '/' + case.case_id)
            checked.append(arm + '/' + case.case_id)
            if 'recall_at_k' in score:
                scores.append(score)
        metrics = summaries[arm]['metrics']
        if metrics['answerableCases'] != len(scores) or metrics['executions'] != len(rows):
            raise ValueError('aggregate execution count mismatch: ' + arm)
        if not scores:
            continue
        for name in ('recall_at_k','precision_at_k'):
            for k in (1,3,5):
                if metrics[f'{name}@{k}'] != statistics.mean(s[name][str(k)] for s in scores):
                    raise ValueError('aggregate score mismatch: ' + arm)
        if metrics['mrr'] != statistics.mean(s['mrr'] for s in scores):
            raise ValueError('aggregate MRR mismatch: ' + arm)
    result = {'status':'PASS', 'executions':len(checked), 'cases':checked,
        'goldIdentityVerified':True, 'productionCodeDigestsVerified':True,
        'evaluationToolDrift':drift, 'exactExecutionHarnessVerified':not drift,
        'scoringEqualAfterJsonRoundTrip':True,'aggregatesVerified':True,'providerCalls':0,'retrievalCalls':0,
        'validation':validation, 'datasetDigest':dataset.dataset_digest,
        'sourceRevision':context.source_revision}
    if args.output:
        private_output(args.output.parent)
        if args.output.exists():
            raise ValueError('do not overwrite prior verification')
        save(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def replay(args):
    manifest, context, dataset, annotations, validation = golden(args.gold_dir, args.media_dir)
    metadata = load(args.run_dir / "run-metadata.json")
    if (metadata["datasetDigest"] != dataset.dataset_digest or
        metadata["sourceRevision"] != context.source_revision or
        metadata["mediaSha256"] != manifest["mediaSha256"] or
        metadata["sidecarSha256"] != digest(args.gold_dir/"annotations.json")):
        raise ValueError("execution/input identity mismatch")
    drift = [name for name, expected in metadata["codeDigests"].items() if digest(ROOT/name) != expected]
    if drift:
        raise ValueError("scoring/execution source changed: " + ", ".join(drift))
    previous = load(args.run_dir / "pilot-metrics.json")
    x3 = {r.case_id: r for r in read_case_results(args.run_dir/"x3"/"results.jsonl")}
    by_case = {c.case_id: c for c in annotations.cases}
    unique_rankings = set()
    checked = []
    for case in dataset.cases:
        ranks = tuple(EvaluationRetrievedEvidence.model_validate(r)
            for r in load(args.run_dir/(case.case_id+".retrieval.json")))
        score = json.loads(json.dumps(score_pilot_retrieval(case, by_case[case.case_id], ranks)))
        stored = {k:v for k,v in previous[case.case_id].items() if k not in {"latency", "human_status"}}
        if score != stored:
            raise ValueError("score changed on identical captured ranks: " + case.case_id)
        metrics = x3[case.case_id].deterministic_metrics
        if "recall_at_k" in score:
            if (score["recall_at_k"] != metrics.retrieval_recall_at_k_by_k or
                score["precision_at_k"] != metrics.retrieval_precision_at_k_by_k or score["mrr"] != metrics.mrr):
                raise ValueError("V1 and X3 retrieval metrics disagree")
        elif metrics.retrieval_recall_at_k is not None or metrics.mrr is not None:
            raise ValueError("no-answer must have no positive retrieval metrics")
        if any(value is not None for value in (metrics.required_fact_exact_coverage,
                metrics.evidence_precision, metrics.schema_valid)):
            raise ValueError("retrieval-only emitted unmeasured answer metrics")
        unique_rankings.add(tuple(r.segment_id for r in ranks))
        checked.append(case.case_id)
    result = {"status":"PASS", "cases":checked, "validation":validation,
        "scoringEqualAfterJsonRoundTrip":True, "agreesWithX3Metrics":True,
        "goldIdentityVerified":True, "executionCodeDigestsVerified":True,
        "retrievalCalls":0, "providerCalls":0, "mediaProcessingCalls":0,
        "distinctStoredRankings":len(unique_rankings),
        "datasetDigest":dataset.dataset_digest, "sourceRevision":context.source_revision,
        "runId":metadata["runId"]}
    if args.output:
        private_output(args.output.parent)
        if args.output.exists():
            raise ValueError("do not overwrite prior verification")
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("gold-dir", "media-dir", "run-dir"):
        p.add_argument("--"+name, type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument('--optimization-run', action='store_true')
    args = p.parse_args()
    (replay_optimization if args.optimization_run else replay)(args)
