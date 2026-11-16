// Local QMD adapter over an explicitly supplied, answer-safe derived corpus.
// No write to the authoritative course repository and no implicit indexing on query.
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';

const [action, runtime, dbPath, corpus, inputPath] = process.argv.slice(2);
if (!['build', 'search'].includes(action) || !runtime || !dbPath || !corpus) {
  throw new Error('usage: build|search runtime dbPath safeCorpus [queryFile]');
}
for (const key of Object.keys(process.env)) {
  if (/API_KEY|TOKEN|SECRET|PASSWORD/.test(key)) delete process.env[key];
}
process.env.QMD_EMBED_MODEL = 'hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf';
process.env.QMD_EMBED_PARALLELISM = '1';
process.env.XDG_CACHE_HOME = path.join(runtime, 'qmd-cache');
process.env.QMD_CONFIG_DIR = path.join(runtime, 'qmd-config');
if (action === 'search') {
  const localModel = path.join(runtime, 'qmd-cache/qmd/models/hf_Qwen_Qwen3-Embedding-0.6B-Q8_0.gguf');
  await fs.access(localModel); // A query must never download a missing model.
  process.env.QMD_EMBED_MODEL = localModel;
}
const { createStore } = await import(pathToFileURL(path.join(runtime, 'qmd/node_modules/@tobilu/qmd/dist/index.js')).href);
const store = await createStore({dbPath, config: {collections: {cs408: {path: corpus, pattern: '**/*.md'}}}});
try {
  if (action === 'build') {
    const start = performance.now();
    const update = await store.update({collections: ['cs408']});
    const embed = await store.embed({force: false});
    process.stdout.write(JSON.stringify({backend: 'qmd', version: '2.8.3', status: 'ready', update, embed,
      elapsed_ms: performance.now() - start, peak_rss_kb: process.resourceUsage().maxRSS, api_request_count: 0}) + '\n');
  } else {
    const queries = JSON.parse(await fs.readFile(inputPath, 'utf8'));
    if (!Array.isArray(queries) || queries.length > 40) throw new Error('invalid_query_batch');
    const rows = [];
    for (const row of queries) {
      if (typeof row.query !== 'string' || row.query.length > 1000) throw new Error('invalid_query');
      const start = performance.now();
      const results = await store.search({queries: [{type: 'lex', query: row.query}, {type: 'vec', query: row.query}],
        collections: ['cs408'], limit: 8, rerank: false,
        filter: {field: 'type', operator: 'eq', value: 'note'}});
      rows.push({id: row.id, elapsed_ms: performance.now()-start,
        candidates: results.map(r => ({id: (r.file || r.displayPath || '').match(/(?:^|\/)((?:DS|CO|OS|CN)\d{2}-\d{2})\.md/i)?.[1]?.toUpperCase(),
          score: r.score, file: r.file || r.displayPath})).filter(r => r.id)});
    }
    process.stdout.write(JSON.stringify({backend: 'qmd', status: 'ready', rows,
      peak_rss_kb: process.resourceUsage().maxRSS, api_request_count: 0})+'\n');
  }
} finally { await store.close(); }
