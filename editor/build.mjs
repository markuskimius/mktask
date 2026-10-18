// npm run build: bundle entry.js into ../mktask/static/vendor/tiptap.js, one
// ES module with no imports, and the licenses of every package inside it
// into tiptap.js.LICENSES.txt beside it. Both are committed, so mktask is
// installed and run without node.

import { build } from "esbuild";
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const outdir = join(here, "..", "mktask", "static", "vendor");
const outfile = join(outdir, "tiptap.js");
mkdirSync(outdir, { recursive: true });

const result = await build({
  entryPoints: [join(here, "entry.js")],
  outfile,
  bundle: true,
  format: "esm",
  target: "es2020",
  minify: true,
  legalComments: "none",   // collected whole below instead
  metafile: true,
  charset: "utf8",
  banner: { js: "// Built from editor/ by `npm run build`; do not edit. Licenses: tiptap.js.LICENSES.txt" },
});

// Every package a bundled file came from, with its license text.
const packages = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const m = input.match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  if (!m || packages.has(m[1])) continue;
  const root = join(here, "node_modules", m[1]);
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const file = readdirSync(root).find((f) => /^(licen[cs]e|copying)/i.test(f));
  packages.set(m[1], { version: pkg.version, license: pkg.license, text: file ? readFileSync(join(root, file), "utf8").trim() : null });
}
const lines = ["Packages bundled into tiptap.js, with their licenses.", ""];
for (const [name, p] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
  lines.push("=".repeat(72), `${name} ${p.version} (${p.license})`, "=".repeat(72), "");
  lines.push(p.text ?? `No license file in the package; package.json says ${p.license}.`, "");
}
writeFileSync(`${outfile}.LICENSES.txt`, lines.join("\n"), "utf8");

const bytes = readFileSync(outfile).length;
console.log(`${outfile}: ${(bytes / 1024).toFixed(0)} KB, ${packages.size} packages`);
if (!existsSync(outfile)) process.exit(1);
