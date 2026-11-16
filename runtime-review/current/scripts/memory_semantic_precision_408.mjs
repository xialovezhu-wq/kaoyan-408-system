// Optional public-concept semantic candidate adapter. No indexing or migrations.
// QMD 2.8.3's installed SDK ignores metadata filters: scope is enforced in SQL
// before cosine distance is evaluated. The existing assessed indexes stay intact.
import fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';

export const SCOPE_SCHEMA = 'cs408-public-semantic-scope-v1';
const PUBLIC_KINDS = new Set(['concept', 'knowledge_point', 'knowledge_subpoint', 'syllabus_entry']);
const SHA = /^[a-f0-9]{64}$/;
const FINGERPRINT = /^[a-f0-9]{6,64}$/;
const MAX_SCOPED_VECTORS = 20000;

function requireCondition(condition, code) {
  if (!condition) throw new Error(code);
}

export function nativeIdentityQuery(query) {
  return typeof query === 'string' && /(?:^|[^a-z0-9])(?:EXAM408-\d{4}-Q\d+(?:-P\d+)?|(?:DS|CO|OS|CN)\d{2}-\d+|(?:DS|CO|OS|CN)_[A-Z0-9_]+|KSP-[A-Z0-9-]+|S27-[A-Z0-9-]+|KLR-[A-Z0-9-]+|EVU?408-[A-Z0-9-]+)(?:$|[^a-z0-9])/i.test(query.normalize('NFKC'));
}

export async function loadRuntime(runtimeRoot) {
  requireCondition(path.isAbsolute(runtimeRoot), 'semantic_runtime_path_invalid');
  const packageRoot = path.join(runtimeRoot, 'qmd/node_modules/@tobilu/qmd');
  const pkg = JSON.parse(await fs.readFile(path.join(packageRoot, 'package.json'), 'utf8'));
  requireCondition(pkg.version === '2.8.3', 'semantic_qmd_version_requires_reverification');
  const dependency = createRequire(path.join(runtimeRoot, 'qmd/package.json'));
  return { packageRoot, dependency, Database: dependency('better-sqlite3'), sqliteVec: dependency('sqlite-vec') };
}

function checkedScope(manifest, allowedIds, kinds) {
  requireCondition(manifest?.schema === SCOPE_SCHEMA && Array.isArray(manifest.documents), 'semantic_scope_manifest_invalid');
  requireCondition(typeof manifest.model === 'string' && manifest.model.length > 0
    && FINGERPRINT.test(manifest.embed_fingerprint || '') && Number.isInteger(manifest.dimensions)
    && manifest.dimensions > 0 && manifest.dimensions <= 4096, 'semantic_embedding_recipe_invalid');
  requireCondition(Array.isArray(allowedIds) && allowedIds.length <= 10000
    && allowedIds.every(id => typeof id === 'string' && id.length > 0), 'semantic_allowed_ids_required');
  const selectedKinds = new Set(kinds ?? [...PUBLIC_KINDS]);
  requireCondition(selectedKinds.size > 0 && [...selectedKinds].every(kind => PUBLIC_KINDS.has(kind)), 'semantic_public_kind_required');
  const byId = new Map();
  for (const document of manifest.documents) {
    requireCondition(typeof document.id === 'string' && document.id.length > 0 && !byId.has(document.id)
      && PUBLIC_KINDS.has(document.kind) && typeof document.collection === 'string'
      && /^[a-z0-9_-]+$/i.test(document.collection) && typeof document.path === 'string'
      && !path.posix.isAbsolute(document.path) && !document.path.split('/').includes('..')
      && !document.path.includes('\\') && SHA.test(document.content_hash || ''), 'semantic_public_document_binding_invalid');
    byId.set(document.id, document);
  }
  requireCondition(allowedIds.every(id => byId.has(id)), 'semantic_scope_not_indexed');
  return [...new Set(allowedIds)].map(id => byId.get(id)).filter(document => selectedKinds.has(document.kind));
}

export function selectAllowedVectorKeys(db, manifest, { allowedIds, kinds } = {}) {
  const documents = checkedScope(manifest, allowedIds, kinds);
  const groups = new Map();
  for (const document of documents) {
    if (!groups.has(document.collection)) groups.set(document.collection, []);
    groups.get(document.collection).push(document);
  }
  const bindings = new Map();
  for (const [collection, items] of groups) {
    for (let offset = 0; offset < items.length; offset += 300) {
      const batch = items.slice(offset, offset + 300);
      const byPath = new Map();
      for (const item of batch) {
        requireCondition(!byPath.has(item.path), 'semantic_scope_duplicate_path');
        byPath.set(item.path, item);
      }
      const placeholders = batch.map(() => '?').join(',');
      // Only ordinary tables are joined. No vector row or distance is touched
      // until the approved identity/type/path/hash set has been resolved.
      const rows = db.prepare(`SELECT d.path, d.hash, cv.seq, cv.total_chunks
        FROM documents d LEFT JOIN content_vectors cv
          ON cv.hash = d.hash AND cv.model = ? AND cv.embed_fingerprint = ?
        WHERE d.active = 1 AND d.collection = ? AND d.path IN (${placeholders})`)
        .all(manifest.model, manifest.embed_fingerprint, collection, ...batch.map(item => item.path));
      const chunks = new Map();
      for (const row of rows) {
        const expected = byPath.get(row.path);
        requireCondition(expected && row.hash === expected.content_hash, 'semantic_index_scope_drift');
        requireCondition(Number.isInteger(row.seq) && row.seq >= 0 && Number.isInteger(row.total_chunks)
          && row.total_chunks > 0, 'semantic_vectors_missing_or_wrong_recipe');
        const state = chunks.get(row.path) ?? { expected: row.total_chunks, seqs: new Set() };
        requireCondition(state.expected === row.total_chunks, 'semantic_chunk_count_conflict');
        state.seqs.add(row.seq);
        chunks.set(row.path, state);
        const key = row.hash + '_' + row.seq;
        if (!bindings.has(key)) bindings.set(key, new Set());
        bindings.get(key).add(expected.id);
      }
      for (const item of batch) {
        const state = chunks.get(item.path);
        requireCondition(state && state.seqs.size === state.expected
          && [...state.seqs].every(sequence => sequence < state.expected), 'semantic_scope_embedding_incomplete');
      }
    }
  }
  requireCondition(bindings.size <= MAX_SCOPED_VECTORS, 'semantic_scope_too_large_for_exact_scan');
  return { bindings, allowedIds: documents.map(document => document.id),
    model: manifest.model, dimensions: manifest.dimensions,
    scopeDocumentCount: documents.length, scopeVectorCount: bindings.size };
}

export function rankSelectedVectors(db, queryVector, scope, { limit = 8 } = {}) {
  requireCondition(Number.isInteger(limit) && limit >= 1 && limit <= 64, 'semantic_limit_invalid');
  requireCondition(Array.isArray(queryVector) || queryVector instanceof Float32Array, 'semantic_query_vector_invalid');
  requireCondition(queryVector.length === scope.dimensions && [...queryVector].every(Number.isFinite)
    && [...queryVector].some(value => value !== 0), 'semantic_query_vector_dimension_or_value_invalid');
  const vector = new Float32Array(queryVector);
  const keys = [...scope.bindings.keys()].sort();
  const distances = new Map();
  let scored = 0;
  for (let offset = 0; offset < keys.length; offset += 400) {
    const chunk = keys.slice(offset, offset + 400);
    // An exact scan over already approved hash_seq keys, with no MATCH/ANN,
    // no global top-k, and no subsequent permission filter.
    const rows = db.prepare(`SELECT hash_seq, vec_distance_cosine(embedding, ?) AS distance
      FROM vectors_vec WHERE hash_seq IN (${chunk.map(() => '?').join(',')})`).all(vector, ...chunk);
    requireCondition(rows.length === chunk.length, 'semantic_vector_rows_missing');
    for (const row of rows) {
      requireCondition(scope.bindings.has(row.hash_seq) && Number.isFinite(row.distance), 'semantic_vector_scope_or_distance_invalid');
      scored += 1;
      for (const identity of scope.bindings.get(row.hash_seq)) {
        if (!distances.has(identity) || row.distance < distances.get(identity)) distances.set(identity, row.distance);
      }
    }
  }
  const candidates = [...distances].sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]))
    .slice(0, limit).map(([id, distance]) => ({ id, score: 1 - distance }));
  return { status: 'ready', candidates, score_kind: 'cosine_similarity_not_truth_or_mastery',
    scope_proof: { permission_filter_stage: 'before_vector_scoring', allowed_document_count: scope.scopeDocumentCount,
      selected_vector_count: scope.scopeVectorCount, scored_vector_count: scored,
      global_ann_used: false, sdk_metadata_filter_used: false, post_filtering_used: false }, index_write_count: 0 };
}

export function rankScopedVectors(db, queryVector, manifest, options = {}) {
  const scope = selectAllowedVectorKeys(db, manifest, options);
  return rankSelectedVectors(db, queryVector, scope, options);
}

async function hashFile(filename) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(filename)) hash.update(chunk);
  return hash.digest('hex');
}

export async function createLocalQueryEmbedder(runtime, manifest, modelPath) {
  requireCondition(path.isAbsolute(modelPath) && SHA.test(manifest.model_sha256 || ''), 'semantic_local_model_binding_required');
  const file = await fs.open(modelPath, 'r');
  try {
    const bytes = Buffer.alloc(4);
    await file.read(bytes, 0, 4, 0);
    requireCondition(bytes.toString('ascii') === 'GGUF', 'semantic_local_model_not_gguf');
  } finally { await file.close(); }
  requireCondition(await hashFile(modelPath) === manifest.model_sha256, 'semantic_local_model_hash_mismatch');
  const { formatQueryForEmbedding } = await import(pathToFileURL(path.join(runtime.packageRoot, 'dist/llm.js')).href);
  const { getEmbeddingFingerprint } = await import(pathToFileURL(path.join(runtime.packageRoot, 'dist/store.js')).href);
  requireCondition(getEmbeddingFingerprint(manifest.model) === manifest.embed_fingerprint, 'semantic_query_recipe_mismatch');
  const llamaModule = await import(pathToFileURL(runtime.dependency.resolve('node-llama-cpp')).href);
  const llama = await llamaModule.getLlama({ build: 'never', skipDownload: true, maxThreads: 2, progressLogs: false, logLevel: 'error' });
  let model, context;
  try {
    model = await llama.loadModel({ modelPath });
    context = await model.createEmbeddingContext({ contextSize: 2048 });
    return {
      async embed(query) {
        requireCondition(!nativeIdentityQuery(query), 'semantic_native_exact_identity_required');
        requireCondition(typeof query === 'string' && query.trim() && query.length <= 1000, 'semantic_query_invalid');
        const formatted = formatQueryForEmbedding(query, manifest.model);
        requireCondition(model.tokenize(formatted).length <= 2000, 'semantic_query_too_long_no_truncation');
        const result = await context.getEmbeddingFor(formatted);
        const vector = Array.from(result.vector);
        requireCondition(vector.length === manifest.dimensions && vector.every(Number.isFinite), 'semantic_model_dimension_mismatch');
        return vector;
      },
      async close() {
        await context.dispose(); await model.dispose(); await llama.dispose();
      },
    };
  } catch (error) {
    if (context) await context.dispose();
    if (model) await model.dispose();
    await llama.dispose();
    throw error;
  }
}

export async function queryVectorFromLocalGguf(runtime, query, manifest, modelPath) {
  const embedder = await createLocalQueryEmbedder(runtime, manifest, modelPath);
  try { return await embedder.embed(query); } finally { await embedder.close(); }
}

export async function searchPublicVectors(request) {
  if (nativeIdentityQuery(request.query)) return { status: 'native_exact_required', candidates: [], model_call_count: 0, index_write_count: 0 };
  requireCondition(request.intent === 'concept' && request.trial === true, 'semantic_public_concept_trial_required');
  requireCondition(path.isAbsolute(request.indexPath), 'semantic_index_path_invalid');
  const runtime = await loadRuntime(request.runtimeRoot);
  const db = new runtime.Database(request.indexPath, { readonly: true, fileMustExist: true });
  try {
    db.pragma('query_only = ON');
    runtime.sqliteVec.load(db);
    const scope = selectAllowedVectorKeys(db, request.manifest, request);
    if (scope.scopeDocumentCount === 0) return { status: 'ready', candidates: [], model_call_count: 0, index_write_count: 0 };
    const vector = await queryVectorFromLocalGguf(runtime, request.query, request.manifest, request.modelPath);
    return { ...rankSelectedVectors(db, vector, scope, request), model_call_count: 1, api_request_count: 0,
      source_version: request.manifest.source_version, projection_sha256: request.manifest.projection_sha256,
      backend: 'qmd_local_query_embedding_scoped_sql', trial_only: true };
  } finally { db.close(); }
}

export async function searchPublicVectorBatch(request) {
  requireCondition(request.intent === 'concept' && request.trial === true && Array.isArray(request.queries)
    && request.queries.length > 0 && request.queries.length <= 40, 'semantic_public_batch_invalid');
  requireCondition(path.isAbsolute(request.indexPath), 'semantic_index_path_invalid');
  const runtime = await loadRuntime(request.runtimeRoot);
  const db = new runtime.Database(request.indexPath, { readonly: true, fileMustExist: true });
  let embedder;
  const started = performance.now();
  try {
    db.pragma('query_only = ON'); runtime.sqliteVec.load(db);
    const rows = [];
    for (const row of request.queries) {
      const start = performance.now();
      if (nativeIdentityQuery(row.query)) {
        rows.push({ id: row.id, status: 'native_exact_required', candidates: [], model_call_count: 0, elapsed_ms: performance.now() - start });
        continue;
      }
      const globalIds = new Set(request.allowedIds ?? []);
      const globalKinds = new Set(request.kinds ?? [...PUBLIC_KINDS]);
      requireCondition(!row.allowedIds || (Array.isArray(row.allowedIds)
        && row.allowedIds.every(identity => globalIds.has(identity))), 'semantic_batch_scope_expansion_forbidden');
      requireCondition(!row.kinds || (Array.isArray(row.kinds)
        && row.kinds.every(kind => globalKinds.has(kind))), 'semantic_batch_kind_expansion_forbidden');
      const options = { ...request, ...row };
      const scope = selectAllowedVectorKeys(db, request.manifest, options);
      if (!scope.scopeDocumentCount) {
        rows.push({ id: row.id, status: 'ready', candidates: [], model_call_count: 0, elapsed_ms: performance.now() - start });
        continue;
      }
      embedder ??= await createLocalQueryEmbedder(runtime, request.manifest, request.modelPath);
      const vector = await embedder.embed(row.query);
      rows.push({ id: row.id, ...rankSelectedVectors(db, vector, scope, options), model_call_count: 1, elapsed_ms: performance.now() - start });
    }
    return { status: 'ready', rows, elapsed_ms: performance.now() - started,
      peak_rss_kib: process.resourceUsage().maxRSS, index_write_count: 0, api_request_count: 0,
      index_projection_hash: request.manifest.projection_sha256, trial_only: true };
  } finally {
    if (embedder) await embedder.close();
    db.close();
  }
}

export async function buildShadow(request) {
  const started = performance.now();
  requireCondition(request.trial === true && path.isAbsolute(request.shadowRoot) && path.isAbsolute(request.corpusPath), 'semantic_shadow_paths_invalid');
  const shadowRoot = path.resolve(request.shadowRoot);
  const indexPath = path.resolve(request.indexPath);
  requireCondition(indexPath.startsWith(shadowRoot + path.sep)
    && indexPath !== path.join(path.resolve(request.runtimeRoot), 'qmd-index.sqlite'), 'semantic_existing_index_write_forbidden');
  const runtime = await loadRuntime(request.runtimeRoot);
  const projection = JSON.parse(await fs.readFile(request.projectionPath, 'utf8'));
  requireCondition(projection.schema === 'cs408-public-semantic-projection-v1' && projection.private_fields_included === false,
    'semantic_public_projection_required');
  const expectedFiles = new Set(projection.documents.map(document => document.id + '.md'));
  const actualFiles = (await fs.readdir(request.corpusPath)).filter(name => name.endsWith('.md'));
  requireCondition(actualFiles.length === expectedFiles.size && actualFiles.every(name => expectedFiles.has(name)), 'semantic_corpus_manifest_mismatch');
  for (const document of projection.documents) {
    requireCondition(PUBLIC_KINDS.has(document.kind) && /^[A-Za-z0-9_-]+$/.test(document.id), 'semantic_public_document_required');
    const raw = await fs.readFile(path.join(request.corpusPath, document.id + '.md'));
    requireCondition(createHash('sha256').update(raw).digest('hex') === document.text_sha256, 'semantic_corpus_text_hash_mismatch');
  }
  await fs.mkdir(shadowRoot, { recursive: true, mode: 0o700 });
  const ownerPath = path.join(shadowRoot, 'shadow-owner.json');
  const owner = { schema: 'cs408-semantic-shadow-owner-v1', projection_sha256: projection.projection_sha256,
    index_path: indexPath, corpus_path: path.resolve(request.corpusPath) };
  let previousOwner;
  try { previousOwner = JSON.parse(await fs.readFile(ownerPath, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (previousOwner) requireCondition(JSON.stringify(previousOwner) === JSON.stringify(owner), 'semantic_shadow_owner_mismatch');
  else {
    try { await fs.access(indexPath); throw new Error('semantic_unowned_index_write_forbidden'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    await fs.writeFile(ownerPath, JSON.stringify(owner), { flag: 'wx', mode: 0o600 });
  }
  const modelPath = path.resolve(request.modelPath);
  const modelSha = await hashFile(modelPath);
  process.env.QMD_EMBED_MODEL = modelPath;
  process.env.QMD_EMBED_PARALLELISM = '1';
  process.env.XDG_CACHE_HOME = path.join(shadowRoot, 'cache');
  process.env.QMD_CONFIG_DIR = path.join(shadowRoot, 'config');
  const { createStore } = await import(pathToFileURL(path.join(runtime.packageRoot, 'dist/index.js')).href);
  const { getEmbeddingFingerprint } = await import(pathToFileURL(path.join(runtime.packageRoot, 'dist/store.js')).href);
  const collection = 'cs408-precision-public';
  const store = await createStore({ dbPath: indexPath, config: {
    models: { embed: modelPath }, collections: { [collection]: { path: request.corpusPath, pattern: '**/*.md' } } } });
  let update, embed, updateMs, embedMs, manifest;
  try {
    let mark = performance.now();
    update = await store.update({ collections: [collection] }); updateMs = performance.now() - mark;
    process.stderr.write(JSON.stringify({ stage: 'public_corpus_indexed', document_count: projection.document_count, update_ms: updateMs }) + '\n');
    let lastProgress = -100;
    mark = performance.now();
    embed = await store.embed({ force: false, collection, onProgress(progress) {
      if (progress.chunksEmbedded - lastProgress >= 100 || progress.chunksEmbedded === progress.totalChunks) {
        lastProgress = progress.chunksEmbedded;
        process.stderr.write(JSON.stringify({ stage: 'public_embeddings', embedded: progress.chunksEmbedded,
          total: progress.totalChunks, errors: progress.errors }) + '\n');
      }
    } });
    embedMs = performance.now() - mark;
    requireCondition(embed.errors === 0, 'semantic_shadow_embedding_incomplete');
    const documents = projection.documents.map(document => {
      const row = store.internal.db.prepare('SELECT hash FROM documents WHERE active=1 AND collection=? AND path=?')
        .get(collection, document.id + '.md');
      requireCondition(row, 'semantic_shadow_document_missing');
      return { id: document.id, kind: document.kind, collection, path: document.id + '.md', content_hash: row.hash };
    });
    const sql = store.internal.db.prepare("SELECT sql FROM sqlite_master WHERE name='vectors_vec'").get()?.sql || '';
    const dimensions = Number(sql.match(/embedding\s+float\[(\d+)\]/i)?.[1]);
    requireCondition(Number.isInteger(dimensions) && dimensions > 0, 'semantic_shadow_vector_schema_unknown');
    manifest = { schema: SCOPE_SCHEMA, source_version: request.sourceVersion ?? null,
      projection_sha256: projection.projection_sha256, model: modelPath, model_sha256: modelSha,
      embed_fingerprint: getEmbeddingFingerprint(modelPath), dimensions, documents, trial_only: true };
    const checked = selectAllowedVectorKeys(store.internal.db, manifest, { allowedIds: documents.map(document => document.id) });
    requireCondition(checked.scopeDocumentCount === documents.length, 'semantic_shadow_scope_verification_failed');
  } finally { await store.close(); }
  const manifestPath = path.join(shadowRoot, 'semantic-scope-manifest.json');
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n', { mode: 0o600 });
  return { status: 'ready', trial_only: true, document_count: manifest.documents.length,
    index_path: indexPath, manifest_path: manifestPath, index_projection_hash: manifest.projection_sha256,
    model_sha256: modelSha, dimensions: manifest.dimensions, update, embed, update_ms: updateMs, embed_ms: embedMs,
    total_ms: performance.now() - started, peak_rss_kib: process.resourceUsage().maxRSS,
    api_request_count: 0, authoritative_source_write_count: 0, previous_index_write_count: 0 };
}

export function validatePublicRerankRow(row, allowedIds) {
  requireCondition(typeof row.query === 'string' && row.query.trim() && row.query.length <= 1000, 'rerank_query_invalid');
  if (nativeIdentityQuery(row.query)) return { native: true, documents: [] };
  requireCondition(Array.isArray(row.documents) && row.documents.length <= 20, 'rerank_max_twenty_candidates');
  const allowed = new Set(allowedIds ?? []);
  requireCondition(Array.isArray(allowedIds), 'rerank_allowed_ids_required');
  const seen = new Set();
  for (const document of row.documents) {
    requireCondition(typeof document.id === 'string' && allowed.has(document.id) && !seen.has(document.id), 'rerank_candidate_scope_or_identity_invalid');
    seen.add(document.id);
    requireCondition(PUBLIC_KINDS.has(document.kind) && typeof document.text === 'string'
      && document.text.length > 0 && document.text.length <= 15000 && SHA.test(document.text_sha256 || '')
      && createHash('sha256').update(document.text).digest('hex') === document.text_sha256,
      'rerank_public_text_binding_invalid');
  }
  return { native: false, documents: row.documents };
}

export async function createLocalPublicReranker(runtime, modelPath, expectedSha) {
  requireCondition(path.isAbsolute(modelPath) && SHA.test(expectedSha || ''), 'rerank_local_model_binding_required');
  requireCondition(await hashFile(modelPath) === expectedSha, 'rerank_local_model_hash_mismatch');
  const handle = await fs.open(modelPath, 'r');
  try {
    const magic = Buffer.alloc(4); await handle.read(magic, 0, 4, 0);
    requireCondition(magic.toString('ascii') === 'GGUF', 'rerank_local_model_not_gguf');
  } finally { await handle.close(); }
  const { getLlama } = await import(pathToFileURL(runtime.dependency.resolve('node-llama-cpp')).href);
  const llama = await getLlama({ build: 'never', skipDownload: true, maxThreads: 2, progressLogs: false, logLevel: 'error' });
  let model, context;
  try {
    model = await llama.loadModel({ modelPath });
    // Same native ranking context used by QMD, but a single bounded context.
    // Errors propagate; QMD's optional constant-0.5 fallback is never accepted.
    context = await model.createRankingContext({ contextSize: 4096, threads: 2 });
    return {
      async rank(query, documents) {
        const queryTokens = model.tokenize(query).length;
        requireCondition(documents.every(document => queryTokens + model.tokenize(document.text).length <= 3072),
          'rerank_input_exceeds_context_no_truncation');
        const scores = await context.rankAll(query, documents.map(document => document.text));
        requireCondition(scores.length === documents.length && scores.every(score => Number.isFinite(score) && score >= 0 && score <= 1),
          'rerank_model_result_invalid');
        return documents.map((document, position) => ({ id: document.id, score: scores[position], input_rank: position + 1 }))
          .sort((a, b) => b.score - a.score || a.input_rank - b.input_rank);
      },
      async close() { await context.dispose(); await model.dispose(); await llama.dispose(); },
    };
  } catch (error) {
    if (context) await context.dispose(); if (model) await model.dispose(); await llama.dispose();
    throw error;
  }
}

export async function rerankPublicBatch(request) {
  requireCondition(request.intent === 'concept' && request.trial === true && SHA.test(request.projectionSha256 || ''),
    'rerank_public_trial_projection_required');
  requireCondition(Array.isArray(request.queries) && request.queries.length > 0 && request.queries.length <= 40,
    'rerank_batch_invalid');
  requireCondition(Number.isInteger(request.limit ?? 8) && (request.limit ?? 8) >= 1 && (request.limit ?? 8) <= 20,
    'rerank_result_limit_invalid');
  // Validate every row before loading a model, not after the first inference.
  const approved = request.queries.map(row => ({ row, checked: validatePublicRerankRow(row, request.allowedIds) }));
  const runtime = await loadRuntime(request.runtimeRoot);
  let reranker;
  const started = performance.now();
  try {
    const rows = [];
    for (const { row, checked } of approved) {
      const start = performance.now();
      if (checked.native) {
        rows.push({ id: row.id, status: 'native_exact_required', candidates: [], model_call_count: 0, elapsed_ms: performance.now() - start });
        continue;
      }
      if (!checked.documents.length) {
        rows.push({ id: row.id, status: 'ready', candidates: [], model_call_count: 0, elapsed_ms: performance.now() - start });
        continue;
      }
      reranker ??= await createLocalPublicReranker(runtime, request.rerankerPath, request.rerankerSha256);
      const candidates = await reranker.rank(row.query, checked.documents);
      rows.push({ id: row.id, status: 'ready', candidates: candidates.slice(0, request.limit ?? 8),
        candidate_count_scored: checked.documents.length, model_call_count: 1, elapsed_ms: performance.now() - start,
        scope_proof: { permission_filter_stage: 'before_reranker', input_public_ids: checked.documents.map(document => document.id),
          candidate_texts_rewritten: false, context_truncation_used: false, constant_fallback_used: false } });
      process.stderr.write(JSON.stringify({ stage: 'public_rerank', completed: rows.length, total: approved.length }) + '\n');
    }
    return { status: 'ready', rows, model: 'Qwen3-Reranker-0.6B-Q8_0', model_sha256: request.rerankerSha256,
      score_kind: 'reranker_relevance_not_truth_or_mastery', projection_sha256: request.projectionSha256,
      elapsed_ms: performance.now() - started, peak_rss_kib: process.resourceUsage().maxRSS,
      api_request_count: 0, index_write_count: 0, source_write_count: 0, trial_only: true };
  } finally { if (reranker) await reranker.close(); }
}

export function loadScopedPublicTexts(db, manifest, { candidateIds, allowedIds, kinds } = {}) {
  requireCondition(Array.isArray(candidateIds) && candidateIds.length <= 20
    && candidateIds.every(identity => typeof identity === 'string'), 'rerank_max_twenty_candidates');
  const allowed = new Set(allowedIds ?? candidateIds);
  requireCondition(candidateIds.every(identity => allowed.has(identity)), 'rerank_candidate_outside_allowed_scope');
  const documents = checkedScope(manifest, [...new Set(candidateIds)], kinds);
  requireCondition(documents.length === new Set(candidateIds).size, 'rerank_candidate_kind_outside_scope');
  const byId = new Map(documents.map(document => [document.id, document]));
  return [...new Set(candidateIds)].map(identity => {
    const document = byId.get(identity);
    // Scope and kind are resolved before a document body is opened.
    const row = db.prepare(`SELECT d.hash, c.doc FROM documents d JOIN content c ON c.hash=d.hash
      WHERE d.active=1 AND d.collection=? AND d.path=?`).get(document.collection, document.path);
    requireCondition(row && row.hash === document.content_hash && typeof row.doc === 'string'
      && createHash('sha256').update(row.doc).digest('hex') === document.content_hash,
      'rerank_sql_public_content_hash_mismatch');
    return { id: document.id, kind: document.kind, text: row.doc, text_sha256: document.content_hash };
  });
}

export async function rerankScopedBatch(request) {
  requireCondition(request.intent === 'concept' && request.trial === true && path.isAbsolute(request.indexPath),
    'rerank_public_scope_required');
  requireCondition(Array.isArray(request.queries) && request.queries.length > 0 && request.queries.length <= 40,
    'rerank_batch_invalid');
  requireCondition(request.expectedProjectionHash === request.manifest?.projection_sha256,
    'rerank_public_projection_stale');
  const runtime = await loadRuntime(request.runtimeRoot);
  const db = new runtime.Database(request.indexPath, { readonly: true, fileMustExist: true });
  let rows;
  try {
    db.pragma('query_only = ON');
    rows = request.queries.map(row => ({ id: row.id, query: row.query,
      documents: nativeIdentityQuery(row.query) ? [] : loadScopedPublicTexts(db, request.manifest,
        { candidateIds: row.candidateIds, allowedIds: request.allowedIds, kinds: request.kinds }) }));
  } finally { db.close(); }
  const allowedIds = request.allowedIds ?? [...new Set(rows.flatMap(row => row.documents.map(document => document.id)))];
  const result = await rerankPublicBatch({ ...request, queries: rows, allowedIds,
    projectionSha256: request.manifest.projection_sha256 });
  return { ...result, index_projection_hash: request.manifest.projection_sha256, sql_public_content_verified: true };
}

export async function probeRerankScope(runtimeRoot) {
  const runtime = await loadRuntime(runtimeRoot);
  const db = new runtime.Database(':memory:');
  try {
    db.exec('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER); CREATE TABLE content(hash TEXT,doc TEXT);');
    const texts = ['选择重传会缓存接收窗口内的失序帧。', '后退N帧通常丢弃失序帧。'];
    const ids = ['CN03-13', 'CN03-12'];
    const documents = texts.map((text, number) => ({ id: ids[number], kind: 'knowledge_point',
      collection: 'fixture', path: ids[number] + '.md', content_hash: createHash('sha256').update(text).digest('hex') }));
    for (let number = 0; number < documents.length; number++) {
      const document = documents[number];
      db.prepare('INSERT INTO documents VALUES(?,?,?,1)').run(document.collection, document.path, document.content_hash);
      db.prepare('INSERT INTO content VALUES(?,?)').run(document.content_hash, number === 0 ? texts[number] : 'corrupted fixture body');
    }
    db.pragma('query_only = ON');
    const manifest = { schema: SCOPE_SCHEMA, model: 'fixture', dimensions: 2, embed_fingerprint: 'abcdef', documents };
    let bodyReads = 0;
    const prepare = db.prepare.bind(db);
    db.prepare = function (sql) { if (sql.includes('c.doc')) bodyReads += 1; return prepare(sql); };
    let outsideRejected = false, driftRejected = false;
    try { loadScopedPublicTexts(db, manifest, { candidateIds: ['CN03-12'], allowedIds: ['CN03-13'] }); }
    catch (error) { outsideRejected = error.message === 'rerank_candidate_outside_allowed_scope'; }
    const outsideReads = bodyReads;
    const selected = loadScopedPublicTexts(db, manifest, { candidateIds: ['CN03-13'], allowedIds: ['CN03-13'] });
    try { loadScopedPublicTexts(db, manifest, { candidateIds: ['CN03-12'], allowedIds: ['CN03-12'] }); }
    catch (error) { driftRejected = error.message === 'rerank_sql_public_content_hash_mismatch'; }
    return { status: 'ready', outside_scope_rejected: outsideRejected, outside_scope_body_reads: outsideReads,
      selected_ids: selected.map(document => document.id), source_hash_drift_rejected: driftRejected,
      fixture_database: ':memory:', model_call_count: 0, source_write_count: 0, production_index_opened: false };
  } finally { db.close(); }
}

export async function probeScope(runtimeRoot) {
  const runtime = await loadRuntime(runtimeRoot);
  const db = new runtime.Database(':memory:');
  runtime.sqliteVec.load(db);
  try {
    db.exec(`CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);
      CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER);
      CREATE VIRTUAL TABLE vectors_vec USING vec0(hash_seq TEXT PRIMARY KEY,embedding float[2] distance_metric=cosine);`);
    const records = [
      { id: 'CN03-13', kind: 'knowledge_point', hash: 'a'.repeat(64), vector: [0.2, 0.98] },
      { id: 'OS03-18', kind: 'knowledge_point', hash: 'b'.repeat(64), vector: [1, 0] },
      { id: 'KSP-CN03-13-FIXTURE', kind: 'knowledge_subpoint', hash: 'c'.repeat(64), vector: [1, 0] },
    ];
    const manifest = { schema: SCOPE_SCHEMA, model: 'fixture-model', embed_fingerprint: 'abcdef', dimensions: 2,
      documents: records.map(row => ({ id: row.id, kind: row.kind, collection: 'fixture', path: row.id + '.md', content_hash: row.hash })) };
    for (const row of records) {
      db.prepare('INSERT INTO documents VALUES(?,?,?,1)').run('fixture', row.id + '.md', row.hash);
      db.prepare('INSERT INTO content_vectors VALUES(?,0,?,?,1)').run(row.hash, manifest.model, manifest.embed_fingerprint);
      db.prepare('INSERT INTO vectors_vec(hash_seq,embedding) VALUES(?,?)').run(row.hash + '_0', new Float32Array(row.vector));
    }
    db.pragma('query_only = ON');
    const result = rankScopedVectors(db, [1, 0], manifest, { allowedIds: ['CN03-13', 'KSP-CN03-13-FIXTURE'], kinds: ['knowledge_point'], limit: 1 });
    requireCondition(result.candidates.length === 1 && result.candidates[0].id === 'CN03-13'
      && result.scope_proof.scored_vector_count === 1, 'semantic_probe_prefilter_failed');
    let unknownRejected = false;
    try { selectAllowedVectorKeys(db, manifest, { allowedIds: ['CN00-99'] }); } catch { unknownRejected = true; }
    return { ...result, installed_qmd_version: '2.8.3', fixture: true, fixture_database: ':memory:',
      closer_disallowed_vectors: 2, unknown_id_rejected: unknownRejected, model_call_count: 0, api_request_count: 0,
      production_index_opened: false, sdk_metadata_filter_supported: false };
  } finally { db.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  let request;
  try {
    let text = '';
    for await (const chunk of process.stdin) text += chunk;
    request = JSON.parse(text);
    requireCondition(['probe_scope', 'probe_rerank_scope', 'build_shadow', 'rerank_scoped_batch', 'query_batch', 'query'].includes(request.operation),
      'semantic_operation_invalid');
    for (const key of Object.keys(process.env)) if (/API_KEY|TOKEN|SECRET|PASSWORD/.test(key)) delete process.env[key];
    const result = request.operation === 'probe_scope' ? await probeScope(request.runtimeRoot)
      : request.operation === 'probe_rerank_scope' ? await probeRerankScope(request.runtimeRoot)
      : request.operation === 'build_shadow' ? await buildShadow(request)
      : request.operation === 'rerank_scoped_batch' ? await rerankScopedBatch(request)
      : request.operation === 'query_batch' ? await searchPublicVectorBatch(request) : await searchPublicVectors(request);
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ status: 'unavailable', reason: error.message, candidates: [],
      index_write_count: request?.operation === 'build_shadow' ? null : 0,
      shadow_index_may_be_partial: request?.operation === 'build_shadow',
      authoritative_source_write_count: 0, previous_index_write_count: 0, api_request_count: 0 }) + '\n');
    process.exitCode = 1;
  }
}
