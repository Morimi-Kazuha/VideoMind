# X3 Golden Dataset v1

This directory contains the X3-A evaluation-only dataset contract artifact.

`golden-dataset-v1.json` contains 16 provenance-backed cases derived from the
existing `work/media/representative-long.mp4` and
`work/media/representative-asr.json` artifacts. The source revision is bound
to the real media identity and extracted ASR observations.

The dataset is explicitly marked `DATASET_INCOMPLETE`: it is an ASR-only,
single-media MVP and does not claim OCR-heavy or multi-media coverage. No
synthetic timestamps, source item IDs, or benchmark scores are included.

This is not a benchmark result and must not be sent to a Provider as-is. A
future X3 runner must use `EvaluationCase.execution_input()` so required
facts, expected evidence, annotations, and reference answers stay outside
the production execution projection.
