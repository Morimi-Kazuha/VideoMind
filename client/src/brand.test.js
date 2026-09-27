import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("current application and design lab use the VideoMind product name", () => {
  for (const path of [
    "../index.html",
    "./App.vue",
    "./AnalysisWorkspace.vue",
    "./design/DesignLab.vue",
  ]) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(source, /VideoMind/);
    assert.doesNotMatch(source, /DOVideo|DOVIDEO/);
  }
});
