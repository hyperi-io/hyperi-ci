// Project:   HyperI CI
// File:      src/hyperi_ci/quality/mermaid_runner.mjs
// Purpose:   Parse mermaid blocks with the real mermaid grammar, no browser
//
// License:   BUSL-1.1 - HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Reads {"blocks":[{"id","text"}]} from the JSON file named in argv[2], writes
// a JSON verdict on stdout, and always exits 0 - the Python caller decides the
// gate, so a non-zero exit here would be a second, competing verdict.
//
// mermaid's bundle reaches for browser globals (DOMPurify calls
// `DOMPurify.addHook`, which is absent without a window), and a VALID flowchart
// throws there - a false failure. linkedom supplies the globals before the
// import, which is what keeps this a parse check rather than the mmdc render
// path and its headless Chrome.

import { readFile } from "node:fs/promises";

const fail = (reason, detail) => {
  process.stdout.write(JSON.stringify({ ok: false, reason, detail }));
  process.exit(0);
};

let mermaid;
try {
  const { parseHTML } = await import("linkedom");
  const dom = parseHTML("<!doctype html><html><body></body></html>");
  // `navigator` is a read-only getter on the Node 24 global, so it is left
  // alone; mermaid's parse path does not read it.
  globalThis.window = dom.window;
  globalThis.document = dom.document;
  globalThis.DOMParser = dom.DOMParser;
  globalThis.Node = dom.Node;
  globalThis.Element = dom.Element;
  globalThis.HTMLElement = dom.HTMLElement;
  globalThis.SVGElement = dom.SVGElement ?? dom.Element;
  mermaid = (await import("mermaid")).default;
} catch (err) {
  fail("missing-dependency", String(err && err.message ? err.message : err));
}

try {
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
} catch (err) {
  fail("initialize-failed", String(err && err.message ? err.message : err));
}

let payload;
try {
  payload = JSON.parse(await readFile(process.argv[2], "utf8"));
} catch (err) {
  fail("bad-input", String(err && err.message ? err.message : err));
}

const results = [];
for (const block of payload.blocks ?? []) {
  try {
    const parsed = await mermaid.parse(block.text, { suppressErrors: false });
    results.push({ id: block.id, ok: true, diagramType: parsed?.diagramType ?? "" });
  } catch (err) {
    const message = String(err && err.message ? err.message : err);
    results.push({ id: block.id, ok: false, error: message });
  }
}

process.stdout.write(JSON.stringify({ ok: true, results }));
