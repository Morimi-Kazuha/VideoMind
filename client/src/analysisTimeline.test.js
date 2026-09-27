import test from "node:test";
import assert from "node:assert/strict";
import {
  evidenceKey,
  formatMediaTime,
  timelineMarkers,
  validEvidenceTime,
} from "./analysisTimeline.js";

test("timeline uses only real seekable evidence timestamps and source fields", () => {
  const hits = [
    { startMs: 0, endMs: 3000, transcript: "hello", ocrTexts: ["slide"] },
    { startMs: 5000, endMs: 7000, transcript: "", ocrTexts: [] },
    { startMs: null, endMs: null, transcript: "missing timestamp" },
    { startMs: 12000, endMs: 13000, transcript: "outside duration" },
  ];
  assert.equal(validEvidenceTime(hits[2]), false);
  const markers = timelineMarkers(hits, 10);
  assert.deepEqual(
    markers.map((marker) => marker.lane),
    ["evidence", "asr", "ocr", "evidence"],
  );
  assert.equal(markers[0].left, 0);
  assert.equal(markers[3].left, 50);
  assert.equal(markers[0].width, 30);
  assert.equal(timelineMarkers(hits, 0).length, 0);
  assert.equal(evidenceKey(hits[0], 3), "0:3000:3");
});

test("time formatting supports minute and hour ranges", () => {
  assert.equal(formatMediaTime(0), "00:00");
  assert.equal(formatMediaTime(125), "02:05");
  assert.equal(formatMediaTime(3661), "01:01:01");
});

test("full video windows remain distinct from query evidence and verified citations", () => {
  const windows = [
    {
      segmentId: "a",
      startMs: 0,
      endMs: 60000,
      transcript: "speech",
      ocrTexts: ["slide"],
    },
    {
      segmentId: "b",
      startMs: 60000,
      endMs: 120000,
      transcript: "later",
      ocrTexts: [],
    },
  ];
  const hits = [
    { startMs: 60000, endMs: 90000, snippet: "query hit", transcript: "later" },
  ];
  const citations = [{ id: "verified", timestampMs: 60000, content: "later" }];
  const markers = timelineMarkers(hits, 120, windows, citations);
  assert.equal(markers.filter((marker) => marker.lane === "asr").length, 2);
  assert.equal(markers.filter((marker) => marker.lane === "ocr").length, 1);
  assert.equal(
    markers.filter((marker) => marker.lane === "evidence").length,
    2,
  );
  assert.equal(
    markers.find((marker) => marker.key === "citation:verified").left,
    50,
  );
});

test("dense windows aggregate to bounded timeline nodes", () => {
  const windows = Array.from({ length: 600 }, (_, index) => ({
    segmentId: String(index),
    startMs: index * 60000,
    endMs: (index + 1) * 60000,
    transcript: "speech",
    ocrTexts: ["screen"],
  }));
  const markers = timelineMarkers([], 36000, windows);
  assert.ok(markers.length <= 240);
  assert.ok(markers.some((marker) => marker.count > 1));
});

test("source observations replace coarse lanes while evidence stays distinct", () => {
  const windows = [
    { segmentId: "window", startMs: 0, endMs: 60000, transcript: "combined" },
  ];
  const observations = [
    { id: "asr-1", kind: "ASR", startMs: 5000, endMs: 6500, text: "first" },
    { id: "ocr-1", kind: "OCR", startMs: 30000, endMs: null, text: "slide" },
  ];
  const markers = timelineMarkers(
    [{ startMs: 5000, endMs: 6500, snippet: "evidence" }],
    60,
    windows,
    [],
    observations,
  );
  assert.deepEqual(
    markers.map((marker) => marker.lane),
    ["asr", "ocr", "evidence"],
  );
  assert.equal(markers[0].key, "observation:asr-1");
  assert.ok(Math.abs(markers[0].left - 5000 / 600) < 0.000001);
  assert.equal(markers[1].left, 50);
  assert.ok(markers.every((marker) => marker.key !== "window:window"));
});

test("a partial observation page keeps the window overview", () => {
  const windows = [
    { segmentId: "first", startMs: 0, endMs: 60000, transcript: "first" },
    { segmentId: "later", startMs: 60000, endMs: 120000, transcript: "later" },
  ];
  const observations = [
    { id: "asr-first", kind: "ASR", startMs: 5000, text: "first" },
  ];
  const markers = timelineMarkers([], 120, windows, [], observations, 300);
  assert.deepEqual(
    markers.map((marker) => marker.key),
    ["window:first", "window:later"],
  );
});
