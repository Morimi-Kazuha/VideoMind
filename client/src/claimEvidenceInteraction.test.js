import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { compileScript, parse } from "@vue/compiler-sfc";
import { createRenderer, nextTick, reactive } from "vue";
import { setAuthToken } from "./api.js";

test("compiled workspace claim button seeks the player and selects the existing timeline citation", async () => {
  const values = new Map();
  globalThis.localStorage = { getItem: (k) => values.get(k) || null,
    setItem: (k, v) => values.set(k, v), removeItem: (k) => values.delete(k) };
  setAuthToken("test-session");
  const claim = '<img src=x onerror="alert(1)">';
  const cite = { id: "verified", claim, source: "ASR", content: "source text",
    timestampMs: 312000, segmentId: "seg", sourceRevision: "r1", sourceItemIds: ["item"] };
  globalThis.fetch = async (path) => new Response(JSON.stringify({ code: 0, message: "ok",
    data: path.includes("agent-citations")
      ? { sourceRevision: "r1", conclusions: [claim, "无来源结论"], citations: [cite] }
      : { items: [], total: 0, available: false, state: "NOT_STARTED" },
  }), { headers: { "content-type": "application/json" } });
  const filename = new URL("./AnalysisWorkspace.vue", import.meta.url);
  const { descriptor } = parse(readFileSync(filename, "utf8"));
  const require = createRequire(import.meta.url);
  let code = compileScript(descriptor, { id: "f1-workspace", inlineTemplate: true }).content;
  code = code.replace(/^import (\w+) from ["'].*\.vue["'];?$/gm,
    (_, name) => `const ${name} = { render() { return null; } };`);
  code = code.replace(/^import ["'].*\.css["'];?$/gm, "");
  code = code.replace(/from (["'])([^"']+)\1/g, (_, quote, specifier) =>
    `from ${JSON.stringify(specifier.startsWith(".") ? new URL(specifier, filename).href
      : pathToFileURL(require.resolve(specifier)).href)}`);
  const component = (await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`)).default;
  const node = (tag = "", text = "") => ({ tag, text, children: [], props: {}, parent: null,
    readyState: 1, duration: 1800, currentTime: 0, play: async () => {}, focus() {},
    addEventListener() {}, removeEventListener() {} });
  const renderer = createRenderer({
    createElement: node, createText: (text) => node("#text", text),
    createComment: (text) => node("#comment", text),
    setText: (n, text) => { n.text = text; },
    setElementText: (n, text) => { n.text = text; n.children = []; },
    patchProp: (n, key, old, value) => { n.props[key] = value; },
    insert: (n, parent, anchor) => {
      if (n.parent) n.parent.children.splice(n.parent.children.indexOf(n), 1);
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      parent.children.splice(index < 0 ? parent.children.length : index, 0, n); n.parent = parent;
    },
    remove: (n) => { if (n.parent) n.parent.children.splice(n.parent.children.indexOf(n), 1); },
    parentNode: (n) => n.parent,
    nextSibling: (n) => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
  });
  const sidebar = reactive({ visible: true, mediaId: 1, generation: 1, goal: "summarize",
    analysisMode: "GENERAL", type: "ai", mode: "result", content: "Markdown",
    playbackUrl: "/video", evidenceResults: [], evidenceQuery: "", followUp: "" });
  const app = renderer.createApp(component, { sidebar, media: { status: "COMPLETED", filename: "video.mp4" },
    actions: { formatPercent: () => "0%", showMessage() {} }, analysisModes: [], goalPresets: [], traceStages: [] });
  const root = node("root");
  app.mount(root);
  try {
    await new Promise((resolve) => setTimeout(resolve, 40)); await nextTick();
    const all = (n) => [n, ...n.children.flatMap(all)];
    const claimSection = all(root).find((n) => n.props.class === "analysis-claims");
    assert.ok(claimSection);
    assert.ok(all(claimSection).some((n) => n.tag === "strong" && n.text === claim));
    assert.ok(!all(claimSection).some((n) => n.tag === "img" || n.props.innerHTML));
    assert.ok(all(claimSection).some((n) => n.text === "暂无通过校验的可绑定证据。"));
    const button = all(claimSection).find((n) => n.tag === "button");
    button.props.onClick(); await nextTick();
    assert.equal(all(root).find((n) => n.tag === "video").currentTime, 312);
    assert.equal(button.props["aria-pressed"], true);
    assert.ok(all(root).some((n) => typeof n.props.class === "string" && n.props.class.includes("is-selected")));
    assert.equal(sidebar.content, "Markdown");
    sidebar.mediaId = 2; sidebar.generation += 1; sidebar.content = "";
    await nextTick();
    assert.ok(!all(root).some((n) => n.props.class === "analysis-claims"));
  } finally { app.unmount(); }
});
