// esbuild bundler for the MV3 extension. Produces IIFE bundles (content/dom-agent) and the
// service worker, copies static assets, and writes dist/manifest.json.
import * as esbuild from 'esbuild';
import { cpSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const dist = join(root, 'dist');
const watch = process.argv.includes('--watch');

if (existsSync(dist)) rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });

const common = { bundle: true, sourcemap: false, target: ['chrome116'], logLevel: 'info', minify: false, legalComments: 'none' };

const builds = [
  { entryPoints: [join(root, 'src/dom/index.ts')], outfile: join(dist, 'dom-agent.js'), format: 'iife' },
  { entryPoints: [join(root, 'src/content/content-script.ts')], outfile: join(dist, 'content-script.js'), format: 'iife' },
  { entryPoints: [join(root, 'src/background/service-worker.ts')], outfile: join(dist, 'service-worker.js'), format: 'esm' },
  { entryPoints: [join(root, 'src/sidepanel/sidepanel.ts')], outfile: join(dist, 'sidepanel.js'), format: 'iife' },
  { entryPoints: [join(root, 'src/options/options.ts')], outfile: join(dist, 'options.js'), format: 'iife' },
];

function copyStatic() {
  for (const f of ['sidepanel.html', 'options.html', 'styles.css']) cpSync(join(root, 'src/static', f), join(dist, f));
  cpSync(join(root, 'src/static/icons'), join(dist, 'icons'), { recursive: true });
  const manifest = JSON.parse(readFileSync(join(root, 'manifest.json'), 'utf8'));
  writeFileSync(join(dist, 'manifest.json'), JSON.stringify(manifest, null, 2));
}

if (watch) {
  const ctxs = await Promise.all(builds.map((b) => esbuild.context({ ...common, ...b })));
  await Promise.all(ctxs.map((c) => c.watch()));
  copyStatic();
  console.log('watching…');
} else {
  await Promise.all(builds.map((b) => esbuild.build({ ...common, ...b })));
  copyStatic();
  console.log('extension built to', dist);
}
