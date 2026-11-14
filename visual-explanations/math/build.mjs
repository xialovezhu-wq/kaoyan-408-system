// Rebuild with the same pinned development package directory as the shared renderer.
import { writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const dev = resolve(process.argv[2] ?? fileURLToPath(new URL('../', import.meta.url)));
const source = fileURLToPath(new URL('.', import.meta.url));
const require = createRequire(resolve(dev, 'package.json'));
const uiVersion = require('@ui-sdk/core/package.json').version;
if (uiVersion !== '0.1.0-beta.1') throw new Error('Use the pinned UI SDK version');
const { build } = require('esbuild');
const result = await build({
  entryPoints: [resolve(source, 'view.js')], nodePaths: [resolve(dev, 'node_modules')],
  absWorkingDir: dev, write: false, bundle: true, minify: true, format: 'iife',
  globalName: 'CodexMathVisual', target: ['es2022'], legalComments: 'inline'
});
const bundle = result.outputFiles[0].text;
if (/<\/script[\s>]/i.test(bundle)) throw new Error('Unsafe inline bundle');
await writeFile(resolve(source, 'math.bundle.js'), bundle);
