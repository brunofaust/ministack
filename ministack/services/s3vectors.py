"""
S3 Vectors Service Emulator.
# guard:allow — stdlib `logging` matches every sibling MiniStack service
# module's own convention (see services/s3tables.py); this is not busydone
# code and does not follow busydone's structlog rule.

Provides BOTH the ``s3vectors`` control plane (vector buckets, vector
indexes, vector bucket policies) and data plane (PutVectors / GetVectors /
DeleteVectors / ListVectors / QueryVectors). Everything is held in memory;
``QueryVectors`` does brute-force cosine/euclidean nearest-neighbor search
using stdlib ``math`` only — MiniStack carries no numpy dependency and this
service must not add one.

Unlike S3 Tables (``services/s3tables.py``, the closest analogue and the
template this module's idioms are copied from), S3 Vectors has NO
ARN-in-path scheme to parse: per botocore's ``s3vectors`` service model
(``rest-json``, apiVersion 2025-07-15, ``signingName: s3vectors``), every
operation is a ``POST`` to a single FIXED path, with the resource identity
(name or ARN) carried entirely in the JSON body:

  POST /CreateVectorBucket         CreateVectorBucket
  POST /GetVectorBucket            GetVectorBucket
  POST /DeleteVectorBucket         DeleteVectorBucket
  POST /ListVectorBuckets          ListVectorBuckets
  POST /PutVectorBucketPolicy      PutVectorBucketPolicy
  POST /GetVectorBucketPolicy      GetVectorBucketPolicy
  POST /DeleteVectorBucketPolicy   DeleteVectorBucketPolicy
  POST /CreateIndex                CreateIndex
  POST /GetIndex                   GetIndex
  POST /DeleteIndex                DeleteIndex
  POST /ListIndexes                ListIndexes
  POST /PutVectors                 PutVectors
  POST /GetVectors                 GetVectors
  POST /DeleteVectors              DeleteVectors
  POST /ListVectors                ListVectors
  POST /QueryVectors               QueryVectors

A vector bucket ARN is ``arn:aws:s3vectors:{region}:{account}:bucket/{name}``;
a vector index ARN is that bucket ARN with ``/index/{indexName}`` appended
(botocore patterns). Every control-plane resource-locating operation accepts
either the bare name(s) or the ARN — ``_resolve_index_identity`` normalizes
both forms the same way ``s3tables._canonical_bucket_arn`` does.

Deleting a vector bucket cascades to its indexes and their vectors (mirrors
``s3tables._delete_table_bucket``'s cascade — a deliberate idiom match, not
an independent design choice: real S3 Vectors requires the bucket be empty
first, but this emulator follows the sibling service's precedent).
"""

import copy
import json
import logging
import math

from ministack.core.arn import ArnParseError, parse_arn
from ministack.core.persistence import PERSIST_STATE, load_state
from ministack.core.responses import (
    AccountRegionScopedDict,
    error_response_json,
    get_account_id,
    get_region,
    json_response,
    now_iso,
)

logger = logging.getLogger("s3vectors")

_VALID_DATA_TYPES = ("float32",)
_VALID_DISTANCE_METRICS = ("euclidean", "cosine")

# ── In-memory state ────────────────────────────────────────

_vector_buckets = AccountRegionScopedDict()   # bucket_name -> bucket dict
_bucket_policies = AccountRegionScopedDict()  # bucket_arn -> policy JSON string
_indexes = AccountRegionScopedDict()          # "bucket_arn\x00index_name" -> index dict
_vectors = AccountRegionScopedDict()          # "index_arn\x00vector_key" -> {"vector": [...], "metadata": {...}}


# ── Persistence ────────────────────────────────────────────

def get_state():
    return {
        "vector_buckets": copy.deepcopy(_vector_buckets),
        "bucket_policies": copy.deepcopy(_bucket_policies),
        "indexes": copy.deepcopy(_indexes),
        "vectors": copy.deepcopy(_vectors),
    }


def restore_state(data):
    _vector_buckets.update(data.get("vector_buckets", {}))
    _bucket_policies.update(data.get("bucket_policies", {}))
    _indexes.update(data.get("indexes", {}))
    _vectors.update(data.get("vectors", {}))


def reset():
    _vector_buckets.clear()
    _bucket_policies.clear()
    _indexes.clear()
    _vectors.clear()


if PERSIST_STATE:
    _saved = load_state("s3vectors")
    if _saved:
        restore_state(_saved)


# ── ARN / identity helpers ─────────────────────────────────

def _bucket_arn(name):
    return f"arn:aws:s3vectors:{get_region()}:{get_account_id()}:bucket/{name}"


def _index_arn(bucket_arn, index_name):
    return f"{bucket_arn}/index/{index_name}"


def _bucket_name_from_arn(bucket_arn):
    try:
        spec = parse_arn(bucket_arn)
    except ArnParseError:
        return None
    if (
        spec.partition != "aws"
        or spec.service != "s3vectors"
        or spec.region != get_region()
        or spec.account_id != get_account_id()
    ):
        return None
    prefix = "bucket/"
    if not spec.resource.startswith(prefix):
        return None
    name = spec.resource[len(prefix):]
    if not name or "/" in name:
        return None
    return name


def _canonical_bucket_arn(bucket_ref):
    if not isinstance(bucket_ref, str) or not bucket_ref:
        return None
    if not bucket_ref.startswith("arn:"):
        return _bucket_arn(bucket_ref)
    name = _bucket_name_from_arn(bucket_ref)
    return _bucket_arn(name) if name else None


def _bucket_ref_from_data(data):
    return data.get("vectorBucketArn") or data.get("vectorBucketName") or ""


def _find_bucket_by_arn(arn):
    arn = _canonical_bucket_arn(arn)
    if not arn:
        return None
    for b in _vector_buckets.values():
        if b["vectorBucketArn"] == arn:
            return b
    return None


def _existing_bucket_arn(bucket_ref):
    arn = _canonical_bucket_arn(bucket_ref)
    if not arn or not _find_bucket_by_arn(arn):
        return None
    return arn


def _bucket_not_found(ref):
    return error_response_json("NotFoundException", f"The vector bucket {ref} does not exist", 404)


def _index_not_found(ref):
    return error_response_json("NotFoundException", f"The vector index {ref} does not exist", 404)


def _index_key(bucket_arn, index_name):
    return f"{bucket_arn}\x00{index_name}"


def _resolve_index_identity(data):
    """Resolve ``(bucket_arn, index_name)`` from either an ``indexArn`` OR a
    ``vectorBucketName``/``vectorBucketArn`` + ``indexName`` pair — the same
    two calling conventions every data-plane and index-scoped control-plane
    operation accepts. Returns ``(None, None)`` when nothing usable was
    supplied; does NOT verify the index actually exists (see ``_find_index``).
    """
    index_arn = data.get("indexArn")
    if index_arn:
        try:
            spec = parse_arn(index_arn)
        except ArnParseError:
            return None, None
        if (
            spec.partition != "aws"
            or spec.service != "s3vectors"
            or spec.region != get_region()
            or spec.account_id != get_account_id()
        ):
            return None, None
        parts = spec.resource.split("/")
        if len(parts) != 4 or parts[0] != "bucket" or parts[2] != "index":
            return None, None
        return _bucket_arn(parts[1]), parts[3]
    index_name = data.get("indexName")
    if not index_name:
        return None, None
    bucket_arn = _canonical_bucket_arn(_bucket_ref_from_data(data))
    return bucket_arn, index_name


def _find_index(bucket_arn, index_name):
    if not bucket_arn or not index_name:
        return None
    return _indexes.get(_index_key(bucket_arn, index_name))


# ── Vector bucket control plane ─────────────────────────────

def _create_vector_bucket(data):
    name = data.get("vectorBucketName", "")
    if not name:
        return error_response_json("ValidationException", "vectorBucketName is required", 400)
    if name in _vector_buckets:
        return error_response_json("ConflictException", f"Vector bucket {name} already exists", 409)
    arn = _bucket_arn(name)
    encryption = data.get("encryptionConfiguration") or {"sseType": "AES256"}
    _vector_buckets[name] = {
        "vectorBucketName": name,
        "vectorBucketArn": arn,
        "creationTime": now_iso(),
        "encryptionConfiguration": encryption,
    }
    logger.info("S3Vectors: created vector bucket %s", name)
    return json_response({"vectorBucketArn": arn})


def _list_vector_buckets(data):
    prefix = data.get("prefix") or ""
    buckets = [
        {"vectorBucketName": b["vectorBucketName"], "vectorBucketArn": b["vectorBucketArn"],
         "creationTime": b["creationTime"]}
        for b in _vector_buckets.values()
        if b["vectorBucketName"].startswith(prefix)
    ]
    return json_response({"vectorBuckets": buckets})


def _get_vector_bucket(data):
    ref = _bucket_ref_from_data(data)
    bucket = _find_bucket_by_arn(ref) if ref else None
    if not bucket:
        return _bucket_not_found(ref or "<unspecified>")
    return json_response({"vectorBucket": bucket})


def _delete_vector_bucket(data):
    ref = _bucket_ref_from_data(data)
    arn = _canonical_bucket_arn(ref)
    if not arn:
        return _bucket_not_found(ref or "<unspecified>")
    name = None
    for n, b in _vector_buckets.items():
        if b["vectorBucketArn"] == arn:
            name = n
            break
    if not name:
        return _bucket_not_found(ref)
    for key in list(_indexes.keys()):
        if not key.startswith(arn + "\x00"):
            continue
        index_arn = _indexes[key]["indexArn"]
        for vkey in list(_vectors.keys()):
            if vkey.startswith(index_arn + "\x00"):
                del _vectors[vkey]
        del _indexes[key]
    _bucket_policies.pop(arn, None)
    del _vector_buckets[name]
    logger.info("S3Vectors: deleted vector bucket %s", name)
    return json_response({})


# ── Vector bucket policy ────────────────────────────────────

def _put_vector_bucket_policy(data):
    ref = _bucket_ref_from_data(data)
    arn = _existing_bucket_arn(ref)
    if not arn:
        return _bucket_not_found(ref or "<unspecified>")
    policy = data.get("policy")
    if not policy:
        return error_response_json("ValidationException", "policy is required", 400)
    _bucket_policies[arn] = policy
    return json_response({})


def _get_vector_bucket_policy(data):
    ref = _bucket_ref_from_data(data)
    arn = _existing_bucket_arn(ref)
    if not arn:
        return _bucket_not_found(ref or "<unspecified>")
    policy = _bucket_policies.get(arn)
    if policy is None:
        return error_response_json("NotFoundException", "The vector bucket policy does not exist", 404)
    return json_response({"policy": policy})


def _delete_vector_bucket_policy(data):
    ref = _bucket_ref_from_data(data)
    arn = _existing_bucket_arn(ref)
    if not arn:
        return _bucket_not_found(ref or "<unspecified>")
    _bucket_policies.pop(arn, None)
    return json_response({})


# ── Vector index control plane ──────────────────────────────

def _create_index(data):
    index_name = data.get("indexName", "")
    if not index_name:
        return error_response_json("ValidationException", "indexName is required", 400)
    bucket_ref = _bucket_ref_from_data(data)
    bucket_arn = _existing_bucket_arn(bucket_ref)
    if not bucket_arn:
        return _bucket_not_found(bucket_ref or "<unspecified>")

    data_type = data.get("dataType", "")
    if data_type not in _VALID_DATA_TYPES:
        return error_response_json("ValidationException", f"Invalid dataType: {data_type!r}", 400)
    dimension = data.get("dimension")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or not (1 <= dimension <= 4096):
        return error_response_json("ValidationException", "dimension must be an integer between 1 and 4096", 400)
    distance_metric = data.get("distanceMetric", "")
    if distance_metric not in _VALID_DISTANCE_METRICS:
        return error_response_json("ValidationException", f"Invalid distanceMetric: {distance_metric!r}", 400)

    key = _index_key(bucket_arn, index_name)
    if key in _indexes:
        return error_response_json("ConflictException", f"Vector index {index_name} already exists", 409)

    bucket_name = _bucket_name_from_arn(bucket_arn) or bucket_arn.rsplit("/", 1)[-1]
    arn = _index_arn(bucket_arn, index_name)
    _indexes[key] = {
        "vectorBucketName": bucket_name,
        "indexName": index_name,
        "indexArn": arn,
        "creationTime": now_iso(),
        "dataType": data_type,
        "dimension": dimension,
        "distanceMetric": distance_metric,
        "metadataConfiguration": data.get("metadataConfiguration"),
        "encryptionConfiguration": data.get("encryptionConfiguration"),
    }
    logger.info("S3Vectors: created index %s/%s", bucket_name, index_name)
    return json_response({"indexArn": arn})


def _get_index(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    return json_response({"index": index})


def _delete_index(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    key = _index_key(bucket_arn, index_name) if bucket_arn and index_name else None
    if not key or key not in _indexes:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    index_arn = _indexes[key]["indexArn"]
    for vkey in list(_vectors.keys()):
        if vkey.startswith(index_arn + "\x00"):
            del _vectors[vkey]
    del _indexes[key]
    logger.info("S3Vectors: deleted index %s", index_arn)
    return json_response({})


def _list_indexes(data):
    bucket_ref = _bucket_ref_from_data(data)
    bucket_arn = _existing_bucket_arn(bucket_ref)
    if not bucket_arn:
        return _bucket_not_found(bucket_ref or "<unspecified>")
    prefix = data.get("prefix") or ""
    result = [
        {"vectorBucketName": idx["vectorBucketName"], "indexName": idx["indexName"],
         "indexArn": idx["indexArn"], "creationTime": idx["creationTime"]}
        for key, idx in _indexes.items()
        if key.startswith(bucket_arn + "\x00") and idx["indexName"].startswith(prefix)
    ]
    return json_response({"indexes": result})


# ── Data plane helpers (ported from tests/e2e/mocks/s3vectors/server.py) ───

def _vector_key(index_arn, key):
    return f"{index_arn}\x00{key}"


def _extract_floats(raw):
    """Normalize a ``VectorData`` union (``{"float32": [...]}}``) or a bare
    list to plain ``list[float]``. Real S3 Vectors wraps both stored vectors
    (``data``) and the query vector (``queryVector``) in this typed envelope;
    a bare list is also accepted for looser callers."""
    if isinstance(raw, dict):
        return list(raw.get("float32", raw.get("Float32", [])))
    if isinstance(raw, list):
        return list(raw)
    return []


def _metadata_matches(metadata, flt):
    """Evaluate an S3 Vectors metadata filter against one vector's metadata.

    Supports ``$eq``/``$ne``/``$in``/``$nin`` predicates and ``$and``/``$or``
    combinators (plus the ``{"field": value}`` shorthand for ``$eq``). An
    empty/None filter matches everything; an unrecognised operator fails
    closed (non-matching) rather than silently returning the whole index.
    """
    if not flt:
        return True
    if "$and" in flt:
        return all(_metadata_matches(metadata, sub) for sub in flt["$and"])
    if "$or" in flt:
        return any(_metadata_matches(metadata, sub) for sub in flt["$or"])
    for field_name, predicate in flt.items():
        if not isinstance(predicate, dict):
            if metadata.get(field_name) != predicate:
                return False
            continue
        for op, expected in predicate.items():
            actual = metadata.get(field_name)
            if op == "$eq":
                if actual != expected:
                    return False
            elif op == "$ne":
                if actual == expected:
                    return False
            elif op == "$in":
                if actual not in expected:
                    return False
            elif op == "$nin":
                if actual in expected:
                    return False
            else:
                return False
    return True


def _cosine_distance(a, b):
    """Cosine distance = ``1 - cosine_similarity``; 0 = identical, 2 = opposite.
    A zero-magnitude vector is treated as maximally dissimilar (distance 1.0)
    rather than raising, since a stored vector could legitimately be all-zero
    for a non-cosine index."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - (dot / (na * nb))


def _euclidean_distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _distance(metric, a, b):
    return _cosine_distance(a, b) if metric == "cosine" else _euclidean_distance(a, b)


# ── Data plane operations ───────────────────────────────────

def _put_vectors(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    vectors = data.get("vectors") or []
    if not vectors:
        return error_response_json("ValidationException", "vectors is required", 400)
    dimension = index["dimension"]
    entries = []
    for v in vectors:
        key = v.get("key", "")
        if not key:
            return error_response_json("ValidationException", "vector key is required", 400)
        floats = _extract_floats(v.get("data"))
        if len(floats) != dimension:
            return error_response_json(
                "ValidationException",
                f"Vector {key!r} has dimension {len(floats)}, expected {dimension}",
                400,
            )
        entries.append((key, floats, v.get("metadata") or {}))
    index_arn = index["indexArn"]
    for key, floats, metadata in entries:
        _vectors[_vector_key(index_arn, key)] = {"vector": floats, "metadata": metadata}
    logger.info("S3Vectors: put %d vector(s) into %s", len(entries), index_arn)
    return json_response({})


def _get_vectors(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    keys = data.get("keys") or []
    return_data = bool(data.get("returnData"))
    return_metadata = bool(data.get("returnMetadata"))
    index_arn = index["indexArn"]
    results = []
    for key in keys:
        entry = _vectors.get(_vector_key(index_arn, key))
        if entry is None:
            continue
        out = {"key": key}
        if return_data:
            out["data"] = {"float32": entry["vector"]}
        if return_metadata:
            out["metadata"] = entry["metadata"]
        results.append(out)
    return json_response({"vectors": results})


def _delete_vectors(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    keys = data.get("keys") or []
    index_arn = index["indexArn"]
    for key in keys:
        _vectors.pop(_vector_key(index_arn, key), None)
    return json_response({})


def _list_vectors(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    return_data = bool(data.get("returnData"))
    return_metadata = bool(data.get("returnMetadata"))
    index_arn = index["indexArn"]
    prefix = index_arn + "\x00"
    results = []
    for key, entry in _vectors.items():
        if not key.startswith(prefix):
            continue
        out = {"key": key[len(prefix):]}
        if return_data:
            out["data"] = {"float32": entry["vector"]}
        if return_metadata:
            out["metadata"] = entry["metadata"]
        results.append(out)
    return json_response({"vectors": results})


def _query_vectors(data):
    bucket_arn, index_name = _resolve_index_identity(data)
    index = _find_index(bucket_arn, index_name)
    if not index:
        return _index_not_found(data.get("indexArn") or index_name or "<unspecified>")
    top_k = data.get("topK")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        return error_response_json("ValidationException", "topK must be a positive integer", 400)
    query_vec = _extract_floats(data.get("queryVector"))
    dimension = index["dimension"]
    if len(query_vec) != dimension:
        return error_response_json(
            "ValidationException",
            f"queryVector has dimension {len(query_vec)}, expected {dimension}",
            400,
        )
    metric = index["distanceMetric"]
    flt = data.get("filter") or {}
    return_metadata = bool(data.get("returnMetadata"))
    return_distance = bool(data.get("returnDistance"))
    index_arn = index["indexArn"]
    prefix = index_arn + "\x00"

    scored = []
    for key, entry in _vectors.items():
        if not key.startswith(prefix):
            continue
        if not _metadata_matches(entry["metadata"], flt):
            continue
        scored.append((key[len(prefix):], _distance(metric, query_vec, entry["vector"]), entry["metadata"]))
    # Lower distance = more similar for both cosine and euclidean distance.
    scored.sort(key=lambda x: x[1])

    results = []
    for vec_key, dist, metadata in scored[:top_k]:
        out = {"key": vec_key}
        if return_distance:
            out["distance"] = dist
        if return_metadata:
            out["metadata"] = metadata
        results.append(out)
    logger.info("S3Vectors: query on %s matched %d, returning %d", index_arn, len(scored), len(results))
    return json_response({"vectors": results, "distanceMetric": metric})


# ── REST router ──────────────────────────────────────────────

_OPERATIONS = {
    "/CreateVectorBucket": _create_vector_bucket,
    "/GetVectorBucket": _get_vector_bucket,
    "/DeleteVectorBucket": _delete_vector_bucket,
    "/ListVectorBuckets": _list_vector_buckets,
    "/PutVectorBucketPolicy": _put_vector_bucket_policy,
    "/GetVectorBucketPolicy": _get_vector_bucket_policy,
    "/DeleteVectorBucketPolicy": _delete_vector_bucket_policy,
    "/CreateIndex": _create_index,
    "/GetIndex": _get_index,
    "/DeleteIndex": _delete_index,
    "/ListIndexes": _list_indexes,
    "/PutVectors": _put_vectors,
    "/GetVectors": _get_vectors,
    "/DeleteVectors": _delete_vectors,
    "/ListVectors": _list_vectors,
    "/QueryVectors": _query_vectors,
}


async def handle_request(method, path, headers, body, query_params):
    if method != "POST":
        return error_response_json(
            "UnknownOperationException", f"Unknown S3Vectors operation: {method} {path}", 400
        )
    data = json.loads(body) if body else {}
    clean = path.rstrip("/")
    handler = _OPERATIONS.get(clean)
    if handler is None:
        return error_response_json(
            "UnknownOperationException", f"Unknown S3Vectors operation: {method} {path}", 400
        )
    return handler(data)
