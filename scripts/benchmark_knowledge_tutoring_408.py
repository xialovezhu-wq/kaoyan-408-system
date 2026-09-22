#!/usr/bin/env python3
"""Read-only point-memory query benchmark. Never writes learner events or builds indexes."""
from __future__ import annotations
import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path
import exam_tutoring_context_408 as context


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def benchmark(repo: Path, iterations: int = 30):
    if not 1 <= iterations <= 500:
        raise ValueError('iterations must be between 1 and 500')
    state = repo / context.DEFAULT_STATE_REL
    manifest = json.loads((state / 'manifest.json').read_text())
    # Setup uses only the existing exact index; timing starts after query input exists.
    exact = json.loads((state / manifest['index_ref']).read_text())
    identities = sorted(exact['profiles'])
    if not identities:
        raise ValueError('empty installed snapshot')
    rows = []
    for i in range(iterations):
        fid = identities[(i * max(1, len(identities) // iterations)) % len(identities)]
        query = {'schema': context.QUERY_INPUT_SCHEMA, 'stage': 'post_answer', 'formal_id': fid}
        started = time.perf_counter()
        result = context.query_context(repo, query)
        elapsed = (time.perf_counter() - started) * 1000
        rows.append({'formal_id': fid, 'query_ms': round(elapsed, 3),
                     'status': result.get('status'), 'bytes': result.get('serialized_bytes'),
                     'summary_count': len(result.get('knowledge_summaries', [])),
                     'truncated': result.get('truncated', False),
                     'coverage': result.get('knowledge_coverage')})
    cli = []
    for fid in identities[:min(3, iterations)]:
        query = {'schema': context.QUERY_INPUT_SCHEMA, 'stage': 'post_answer', 'formal_id': fid}
        started = time.perf_counter()
        response = subprocess.run([sys.executable, str(Path(context.__file__).resolve()), 'query',
            '--repo', str(repo), '--input-json', json.dumps(query)], capture_output=True, text=True,
            check=True, timeout=10)
        elapsed = (time.perf_counter() - started) * 1000
        result = json.loads(response.stdout)
        cli.append({'formal_id': fid, 'cli_ms': round(elapsed, 3), 'status': result.get('status')})
    durations = [r['query_ms'] for r in rows]
    return {'schema': 'knowledge_tutoring_benchmark_408_v1', 'source_version': manifest.get('source_version'),
        'sample_count': len(rows), 'function_median_ms': statistics.median(durations),
        'function_p95_ms': percentile(durations, .95), 'function_max_ms': max(durations),
        'cli_samples': cli, 'samples': rows,
        'all_queries_ready': all(r['status'] == 'ready' for r in rows + cli),
        'local_query_budget_ms': 100, 'local_cli_budget_ms': 250,
        'local_budget_pass': percentile(durations, .95) <= 100 and all(r['cli_ms'] <= 250 for r in cli),
        'actual_model_ttft_measured': False, 'model_calls': 0, 'learning_writes': 0,
        'measurement_boundary': 'local query only; excludes model inference, tool round trip and UI rendering'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = benchmark(args.repo.resolve(), args.iterations)
    raw = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw)
    print(raw, end='')

if __name__ == '__main__':
    main()
