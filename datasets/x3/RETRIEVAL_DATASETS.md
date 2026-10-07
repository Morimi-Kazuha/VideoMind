# Retrieval-focused synthetic probes

Both versions use the existing X3 EvaluationDataset / EvaluationCase contracts.
Each contains twelve SYNTHETIC cases over thirty canonical one-minute segments,
covering paraphrase, exact technical terms, OCR-only, ASR-only, boundary crossing
and distractors. They are separate from historical golden-dataset-v1/v2.

`retrieval-focused-v1` preserves the initial exploratory fixture/results. Its
second boundary case placed text saying 24:50/25:20 in observations starting at
25:00/26:00, so it was not actually across a fixed 25:00 chunk boundary.

`retrieval-focused-v2` corrects those observation minutes to 24:00/25:00 and is
the delivery dataset. This changes the source revision and dataset digest.
The tool never overwrites an existing dataset or a nonempty output directory.
The aggregate metrics happened to remain identical; no algorithm tuning was
performed to make the correction improve the outcome.

The committed source fixtures contain synthetic text and annotations. Runtime
adapters receive only each case's execution_input and prepared VideoContext,
never the fixture's target minutes or other gold annotations. Both comparison
arms use local TF-IDF/summary/planner. This is not a live semantic-model test,
cross-encoder validation, production benchmark or no-answer/abstention study.
