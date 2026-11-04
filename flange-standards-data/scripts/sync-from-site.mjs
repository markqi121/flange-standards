#!/usr/bin/env node
/**
 * Sync the flange-standard data out of the Website project into this package.
 *
 * The Website (../src/data/standards) remains the editing source. Running this
 * script copies every dimension and reference JSON file into `data/` and
 * regenerates `data/standards.json` from the TypeScript standard metadata.
 *
 * Usage:  node scripts/sync-from-site.mjs
 *
 * Requires: Node >= 18 and the Website's node_modules (esbuild is bundled with
 * Astro/Vite). The package itself has no runtime dependencies.
 */
import { build } from 'esbuild';
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.resolve(scriptDir, '..');
const siteRoot = path.resolve(pkgRoot, '..');

const siteData = path.join(siteRoot, 'src', 'data', 'standards');
const tmpDir = path.join(pkgRoot, '.tmp');

const DIMENSION_FILES = [
  'asme-b16-5.json',
  'asme-b16-47-a.json',
  'asme-b16-47-b.json',
  'jis-b2220.json',
  'en-1092-1.json',
];

const REFERENCE_FILES = [
  'asme-b16-5.json',
  'asme-b16-47.json',
  'en-1092-1.json',
  'jis-b2220.json',
];

function ensureDir(dir) {
  mkdirSync(dir, { recursive: true });
}

function checkSource() {
  const required = [
    path.join(siteData, 'index.ts'),
    path.join(siteData, 'reference', 'index.ts'),
    ...DIMENSION_FILES.map((f) => path.join(siteData, f)),
    ...REFERENCE_FILES.map((f) => path.join(siteData, 'reference', f)),
  ];
  const missing = required.filter((file) => {
    try {
      readFileSync(file);
      return false;
    } catch {
      return true;
    }
  });
  if (missing.length > 0) {
    throw new Error(
      `Website source not found. Expected the package inside the Website repo ` +
        `(../src/data/standards). Missing:\n  ${missing.join('\n  ')}`,
    );
  }
}

/** Bundle the TypeScript metadata modules with esbuild and evaluate them. */
async function readSiteMetadata() {
  ensureDir(tmpDir);

  const entryPath = path.join(tmpDir, 'metadata-entry.js');
  const outfile = path.join(tmpDir, 'metadata-bundle.mjs');

  const dimensionIndex = path.join(siteData, 'index.ts');
  const referenceIndex = path.join(siteData, 'reference', 'index.ts');

  writeFileSync(
    entryPath,
    [
      `export * from ${JSON.stringify(dimensionIndex)};`,
      `export { referenceStandardInfo, referenceTopicLabels, referenceTopicKeys } from ${JSON.stringify(referenceIndex)};`,
      '',
    ].join('\n'),
    'utf8',
  );

  await build({
    entryPoints: [entryPath],
    outfile,
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node18',
    logLevel: 'silent',
    loader: { '.json': 'json' },
  });

  const mod = await import(`${pathToFileURL(outfile).href}?t=${Date.now()}`);
  return {
    standardMeta: mod.standardMeta,
    standardIds: mod.standardIds,
    referenceStandardInfo: mod.referenceStandardInfo,
    referenceTopicLabels: mod.referenceTopicLabels,
    referenceTopicKeys: mod.referenceTopicKeys,
  };
}

function buildStandardsJson(meta) {
  const { standardMeta, standardIds, referenceStandardInfo, referenceTopicLabels } = meta;

  const standards = standardIds.map((id) => ({
    ...standardMeta[id],
    reference: referenceStandardInfo[id],
    // ASME B16.47 Series A/B share the single `asme-b16-47` reference document.
    referenceDocumentId: id.startsWith('asme-b16-47') ? 'asme-b16-47' : id,
  }));

  // ASME B16.47 has one reference document shared by Series A and Series B.
  const extraReferenceStandards = Object.values(referenceStandardInfo).filter(
    (info) => !standardIds.includes(info.id),
  );

  const referenceStandards = Object.values(referenceStandardInfo);

  return {
    generatedFrom: 'Website src/data/standards/index.ts and reference/index.ts',
    standards,
    referenceStandards,
    extraReferenceStandards,
    topicLabels: referenceTopicLabels,
  };
}

function copyData() {
  const dimsDir = path.join(pkgRoot, 'data', 'dimensions');
  const refDir = path.join(pkgRoot, 'data', 'reference');
  ensureDir(dimsDir);
  ensureDir(refDir);

  for (const file of DIMENSION_FILES) {
    copyFileSync(path.join(siteData, file), path.join(dimsDir, file));
  }
  for (const file of REFERENCE_FILES) {
    copyFileSync(path.join(siteData, 'reference', file), path.join(refDir, file));
  }
}

async function main() {
  checkSource();

  const meta = await readSiteMetadata();
  copyData();

  const standardsJson = buildStandardsJson(meta);
  writeFileSync(
    path.join(pkgRoot, 'data', 'standards.json'),
    `${JSON.stringify(standardsJson, null, 2)}\n`,
    'utf8',
  );

  const counts = {
    dimensions: standardsJson.standards.length,
    referenceDocs: REFERENCE_FILES.length,
  };
  console.log(
    `Synced ${counts.dimensions} dimension datasets and ${counts.referenceDocs} reference documents into ${path.relative(process.cwd(), pkgRoot)}/data`,
  );
  console.log('Next: python3 scripts/build.py');
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
