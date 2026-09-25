# ASR source item granularity

The 60 second audio files are processing units. Whisper returns timed segments
within each file. `x2-a-v3` retains those source grounded spans as individual
ASR observations and offsets their times by the audio file's position. The
60 second `VideoContext` windows remain the same. The extraction contract is
`video-context-whisper-spans-v2`; historical v1 and v2 revisions are unchanged.

An offline temporal audit of frozen golden dataset v1 found that its seven
distinct ASR source item locations collapsed into two 60 second production
items. Four of 16 cases had two separate references collapse into one item.
The new representative media pass produced 52 ASR items and one OCR item.
Two passes from separate UUID workspaces agreed on source revision
`45d7e5513af70f05c36ffeb04d981948d2cec85c874ff6f4ac5b5f0e83ca47bd`,
segment IDs, and source item IDs. Each of the seven old distinct temporal
locations now has a distinct best-overlap ASR item. Median duration inflation
is 1.0 and maximum is 2.16. One old eight second interval overlaps two new
Whisper items almost evenly, so any dataset rebase must preserve that
ambiguity rather than select one by query meaning.

These are provenance structure measurements, not model evaluation results.
The audit used only source type and time interval overlap; no query, expected
answer, difficulty label, or model output influenced the mapping.
