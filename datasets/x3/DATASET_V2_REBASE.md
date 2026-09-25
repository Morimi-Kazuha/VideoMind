# Golden dataset v2 rebase

V1 remains unchanged: raw file SHA-256
`e318193a681ed2794808713a4d1a9dd723b429ad030aee66ecd1bdcde488c2bc`,
logical dataset digest
`d28efd2e639ab836ed85a4de40c64ba5a39bb9baa5878031b4a4b350a88aa25f`.
Its prepared source revision is orphaned in reachable persistence. Its OCR
provenance also depended on an ephemeral input path.

V2 uses the same media bytes and 16 cases. Two independent local preparation
passes produced source revision
`45d7e5513af70f05c36ffeb04d981948d2cec85c874ff6f4ac5b5f0e83ca47bd`
with identical segment and source item IDs. V2's logical dataset digest is
`7f374396c5eb002ba158717afaffa8a65f5ee64ed79663f1f6b6047960dae8e4`.
It remains `DATASET_INCOMPLETE`: no OCR-heavy or multi-media cases were added.

The rebase used only source type and temporal overlap. Repeated v1 reference
names sharing one old source item keep their distinct `refId` and required
fact links. All six unambiguous old ASR items map to the best overlapping new
Whisper item. Times below are milliseconds.

| V1 evidence location | V1 interval | V2 interval | Match |
| --- | ---: | ---: | --- |
| opening-clause | 0–8000 | 0–8000 | SOURCE_ITEM |
| opening-request | 7740–10740 | 8000–11000 | SOURCE_ITEM |
| poem-start / poem-magic | 305800–308040 | 304960–308080 | SOURCE_ITEM |
| wind-sea | 315000–318360 | 315080–318400 | SOURCE_ITEM |
| pool-safe | 326040–329280 | 326120–333120 | SOURCE_ITEM |
| poem-sea | 329280–337280 | 329280–337280 | TEMPORAL_REGION |
| poem-end | 338580–342280 | 338920–342120 | SOURCE_ITEM |

`poem-sea` overlaps two new Whisper items almost evenly (about 48% and 52%).
V2 preserves its old temporal interval as a region in three cases. This can
match either overlapping ASR item, so its retrieval metric is less specific
than the six source item mappings. Selecting one using query meaning would
introduce a subjective mapping; this uncertainty is explicit in the artifact.

Queries, modes, case IDs, required facts, answer variants, and runtime inputs
are unchanged. No model outputs, difficulty labels, or fixed-lane results
influenced the rebase. V2 must be prepared and frozen before any formal X3-C
execution.
