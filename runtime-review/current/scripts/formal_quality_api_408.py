"""Bounded OpenAI calls and durable, per-run response evidence for 408 intake."""
from __future__ import annotations

from array import array
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
import uuid

import runtime_paths_408 as _paths

EMBEDDING = "text-embedding-3-large"
DIMENSIONS = 1536
DECISIONS = "gpt-6-luna"
MODELS = {"embeddings": EMBEDDING, "decisions": DECISIONS}
RETRYABLE = {408, 409, 429, 499, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BRIDGE = _paths.bridge_root() / 'study_openai.py'
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")


class QualityError(RuntimeError):
    """A bounded stage stopped; its original scope can be resumed."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path):
    if path.is_symlink():
        raise QualityError("quality_object_symlink")
    return json.loads(path.read_bytes())


def atomic(path, value):
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".quality-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_transport():
    if not BRIDGE.is_file():
        raise QualityError("quality_openai_transport_missing")
    if str(BRIDGE.parent) not in sys.path:
        sys.path.insert(0, str(BRIDGE.parent))
    spec = importlib.util.spec_from_file_location("study_openai_quality_408", BRIDGE)
    if spec is None or spec.loader is None:
        raise QualityError("quality_openai_transport_missing")
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    return transport.request


def client_id(value):
    return value if isinstance(value, str) and SAFE_REQUEST_ID.fullmatch(value) else None


def wait_seconds(value):
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def vector(value):
    if (not isinstance(value, list) or len(value) != DIMENSIONS or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in value)):
        raise QualityError("quality_vector_invalid")
    norm = math.hypot(*value)
    if not math.isfinite(norm) or not norm:
        raise QualityError("quality_vector_norm_invalid")
    return [x / norm for x in value]


def vector_hex(value):
    result = array("f", value)
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes().hex()


def read_vector(hex_bytes):
    result = array("f")
    result.frombytes(bytes.fromhex(hex_bytes))
    if sys.byteorder != "little":
        result.byteswap()
    if len(result) != DIMENSIONS or any(not math.isfinite(x) for x in result) or not any(result):
        raise QualityError("quality_stored_vector_invalid")
    return result


def _success_rows(endpoint, response, request_rows):
    requested = list(request_rows)
    name_field = "index" if endpoint == "embeddings" else "name"
    response_rows = response.get("data" if endpoint == "embeddings" else "answers")
    if not isinstance(response_rows, list):
        return {}
    valid = {}
    seen = set()
    for row in response_rows:
        if not isinstance(row, dict):
            continue
        name = row.get(name_field)
        if endpoint == "embeddings":
            if type(name) is not int or not 0 <= name < len(requested):
                raise QualityError("quality_invalid_embedding_index")
            name = requested[name]
        if not isinstance(name, str) or name not in request_rows:
            raise QualityError("quality_unrequested_response_unit")
        if name in seen:
            raise QualityError("quality_duplicate_response_unit")
        seen.add(name)
        if endpoint == "embeddings":
            try:
                valid[name] = vector_hex(vector(row.get("embedding")))
            except (QualityError, ValueError, OverflowError):
                continue
        else:
            p = row.get("probability")
            if row.get("type") == "predicate" and type(p) in (int, float) and 0 <= p <= 1:
                valid[name] = float(p)
    return valid


class Journal:
    def __init__(self, root, run_id, *, request=None, seconds=900):
        self.root = Path(root) / "requests"
        self.run_id = run_id
        self.mode = "live" if request is None else "mock"
        self.request = request if request is not None else load_transport()
        self.deadline = time.monotonic() + seconds

    def stage(self, purpose, endpoint, rows, *, evidence=None):
        """rows maps stable IDs to text or predicate instructions."""
        if not rows:
            return {}
        stage_root = self.root / purpose
        results = {}
        expected = {key: digest([endpoint, MODELS[endpoint], DIMENSIONS if endpoint == "embeddings" else None,
                                 evidence, value]) for key, value in rows.items()}
        attempts = dict.fromkeys(rows, 0)
        for path in sorted(stage_root.glob("*.json")):
            prior = read(path)
            if (prior.get("run_id"), prior.get("purpose"), prior.get("endpoint"), prior.get("mode")) != (
                    self.run_id, purpose, endpoint, self.mode):
                continue
            for key, value in prior["unit_input_sha256"].items():
                if expected.get(key) == value and key in expected:
                    attempts[key] += 1
                    if key in prior.get("results", {}):
                        results[key] = {"value": prior["results"][key], "request": path.stem,
                                        "input_sha256": value, "reused": True}

        def perform(batch):
            pending = dict(batch)
            output = {}
            # Retry budget is per invocation; a later explicit resume is a new
            # bounded invocation and reuses every completed unit from this run.
            for retry in range(MAX_ATTEMPTS):
                remaining = self.deadline - time.monotonic()
                if remaining < 1:
                    break
                request_key = "q408-" + uuid.uuid4().hex
                row_inputs = {key: expected[key] for key in pending}
                if endpoint == "embeddings":
                    payload = {"model": EMBEDDING, "dimensions": DIMENSIONS, "encoding_format": "float",
                               "input": list(pending.values())}
                else:
                    payload = {"model": DECISIONS, "input": evidence, "questions": [
                        {"type": "predicate", "name": key, "instructions": value}
                        for key, value in pending.items()]}
                receipt = {"schema": "cs408-quality-request-v1", "run_id": self.run_id, "purpose": purpose,
                           "endpoint": endpoint, "mode": self.mode, "client_request_id": request_key,
                           "unit_input_sha256": row_inputs, "input_sha256": digest(payload),
                           "model_requested": MODELS[endpoint], "status": "OUTCOME_UNKNOWN",
                           "attempt": max(attempts[key] for key in pending) + retry + 1,
                           "started_at": datetime.now(timezone.utc).isoformat()}
                path = stage_root / (request_key + ".json")
                atomic(path, receipt)
                delay = 1.0 + 2.0 * retry
                can_retry = True
                try:
                    response = self.request(endpoint, payload, timeout=min(45, remaining),
                                            client_request_id=request_key)
                    receipt.update(response_sha256=digest(response), request_id=client_id(response.get("_request_id")),
                                   model_returned=response.get("model"), usage=response.get("usage"))
                    model = response.get("model")
                    if (not isinstance(model, str) or (model != MODELS[endpoint]
                        and not re.fullmatch(re.escape(MODELS[endpoint]) + r"-\d{4}-\d{2}-\d{2}", model))):
                        raise QualityError("quality_provider_model_mismatch")
                    if not isinstance(response.get("usage"), dict):
                        raise QualityError("quality_provider_usage_missing")
                    valid = _success_rows(endpoint, response, pending)
                    receipt.update(results=valid, missing=sorted(set(pending) - valid.keys()),
                                   status="OK" if len(valid) == len(pending) else "PARTIAL")
                    for key, value in valid.items():
                        output[key] = {"value": value, "request": request_key,
                                       "input_sha256": expected[key], "reused": False}
                        pending.pop(key)
                except Exception as exc:
                    code = getattr(exc, "code", str(exc) if isinstance(exc, QualityError) else type(exc).__name__)
                    status_code = re.fullmatch(r"STUDY_API_HTTP_(\d{3})", code)
                    can_retry = bool(status_code and int(status_code[1]) in RETRYABLE)
                    provider_code = getattr(exc, "error_code", None)
                    if provider_code in {"insufficient_quota", "billing_hard_limit_reached"}:
                        can_retry = False
                    if code == "STUDY_API_UNAVAILABLE":
                        can_retry = True
                    after = wait_seconds(getattr(exc, "retry_after", None))
                    if after is not None:
                        delay = max(delay, after)
                    receipt.update(status="OUTCOME_UNKNOWN" if code == "STUDY_API_UNAVAILABLE" else "FAILED",
                                   error=code, provider_error=provider_code,
                                   request_id=client_id(getattr(exc, "request_id", None)) or receipt.get("request_id"))
                receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
                atomic(path, receipt)
                if not pending or not can_retry or retry + 1 == MAX_ATTEMPTS or delay > 30:
                    break
                if time.monotonic() + delay + 1 >= self.deadline:
                    break
                time.sleep(delay)
            return output

        chunks, batch, size = [], [], 0
        for key, value in rows.items():
            if key in results:
                continue
            width = len(value.encode())
            if width > 7_000:
                raise QualityError("quality_unit_text_requires_structured_split")
            if batch and (len(batch) >= 32 or size + width > 60_000):
                chunks.append(batch)
                batch, size = [], 0
            batch.append((key, value))
            size += width
        if batch:
            chunks.append(batch)
        if chunks:
            with ThreadPoolExecutor(max_workers=2) as worker:
                for output in worker.map(perform, chunks):
                    results.update(output)
        if missing := sorted(rows.keys() - results.keys()):
            raise QualityError(f"quality_{purpose}_pending:{','.join(missing[:12])}:{len(missing)}")
        return results
