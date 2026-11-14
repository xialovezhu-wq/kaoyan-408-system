import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Dependencies live in an explicit development directory. Rendering uses the
// checked bundles and Python; it doesn't use npm or download anything.
const dev = resolve(process.argv[2] ?? fileURLToPath(new URL('.', import.meta.url)));
const source = fileURLToPath(new URL('.', import.meta.url));
const require = createRequire(resolve(dev, 'package.json'));
const { build } = require('esbuild');
for (const [name, globalName] of [['view', 'CodexVisualView'], ['a2ui', 'CodexVisualA2ui']]) {
  const result = await build({
    entryPoints: [resolve(source, name + '.js')],
    nodePaths: [resolve(dev, 'node_modules')],
    absWorkingDir: dev,
    write: false,
    bundle: true,
    minify: true,
    format: 'iife',
    globalName,
    target: ['es2022'],
    legalComments: 'inline',
  });
  const bundle = result.outputFiles[0].text;
  if (/<\/script[\s>]/i.test(bundle)) throw new Error('Bundle cannot be placed in an inline script');
  const destination = resolve(source, name + '.bundle.js');
  await writeFile(destination, bundle);
  console.log(name, Buffer.byteLength(await readFile(destination)));
}
