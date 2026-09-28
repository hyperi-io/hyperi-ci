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

//
// Packages are looked up in the node_modules directories named by
// HYPERCI_NODE_MODULES (path-delimiter separated), in order. The ESM loader
// ignores NODE_PATH, and a bare import from here would search site-packages.

import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { delimiter, dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const fail = (reason, detail) => {
  process.stdout.write(JSON.stringify({ ok: false, reason, detail }));
  process.exit(0);
};

const moduleDirs = (process.env.HYPERCI_NODE_MODULES ?? "")
  .split(delimiter)
  .filter(Boolean);

// The ESM entry of an `exports` / `module` / `main` declaration.
const esmEntry = (spec) => {
  if (typeof spec === "string") return spec;
  if (spec && typeof spec === "object") {
    return esmEntry(spec["."] ?? spec.import ?? spec.default);
  }
  return undefined;
};

const load = async (name) => {
  for (const dir of moduleDirs) {
    let manifest;
    try {
      // Resolving from a file directly inside `dir` searches `dir` itself.
      manifest = createRequire(join(dir, "hyperi-ci.js")).resolve(
        `${name}/package.json`,
      );
    } catch {
      continue;
    }
    const pkg = JSON.parse(await readFile(manifest, "utf8"));
    const entry = esmEntry(pkg.exports) ?? pkg.module ?? pkg.main ?? "index.js";
    return import(pathToFileURL(join(dirname(manifest), entry)).href);
  }
  return import(name);
};

let mermaid;
try {
  const { parseHTML } = await load("linkedom");
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
  mermaid = (await load("mermaid")).default;
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
