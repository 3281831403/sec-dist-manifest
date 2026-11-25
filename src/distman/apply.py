"""Incremental distribution manifests: parse, diff, apply and verify.

A manifest is a JSON object with a version, a digest algorithm and a file
table.  A delta names the version it produces and the file-level differences
needed to produce it.  The helpers in this module keep the file table small
and give callers a single place to build and consume deltas.

Only the Python standard library is used.  No function reads the filesystem,
the clock or any network resource.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def _check_algorithm(value):
    """Return a normalised supported hash name or raise ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("algorithm must be a non-empty string")
    algorithm = value.strip().lower()
    try:
        probe = hashlib.new(algorithm)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported digest algorithm: " + value) from exc
    if not getattr(probe, "digest_size", 0):
        raise ValueError("unsupported digest algorithm: " + value)
    return algorithm


def normalize_path(path):
    """Return the portable form of a relative path.

    Distribution manifests are written on several platforms, so native
    Windows separators are converted to the forward slash form used by the
    manifest itself.
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not path:
        raise ValueError("path must not be empty")
    return path.replace("\\", "/")


def _canonical_files(files):
    """Validate a file table and return a fresh copy keyed by path."""
    if not isinstance(files, Mapping):
        raise ValueError("files must be a mapping")
    result = {}
    for raw_path, digest in files.items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        result[path] = digest
    return result


def _manifest_parts(manifest):
    """Validate a manifest and return version, algorithm and file table."""
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a mapping")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest version must be a non-empty string")
    algorithm = _check_algorithm(manifest.get("algorithm"))
    files = manifest.get("files")
    return version, algorithm, _canonical_files(files)


def _canonical_payload(version, algorithm, files):
    """Build the canonical byte string covered by the manifest digest."""
    lines = ["version=" + version, "algorithm=" + algorithm]
    for path in sorted(files):
        lines.append("file=" + path + "=" + files[path])
    return "\n".join(lines).encode("utf-8")


def _manifest_digest(manifest):
    """Return the digest that identifies a complete manifest."""
    version, algorithm, files = _manifest_parts(manifest)
    return hashlib.sha256(
        _canonical_payload(version, algorithm, files)
    ).hexdigest()


def parse_manifest(text):
    """Parse a JSON manifest into version, algorithm and canonical files."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    if not isinstance(text, str):
        raise ValueError("manifest text must be a string")
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise ValueError("manifest must be a JSON object")

    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest version must be a non-empty string")
    algorithm = _check_algorithm(data.get("algorithm"))

    raw_files = data.get("files")
    if isinstance(raw_files, Mapping):
        items = list(raw_files.items())
    elif isinstance(raw_files, list):
        items = []
        for item in raw_files:
            if not isinstance(item, Mapping):
                raise ValueError("each file entry must be an object")
            if "path" not in item or "digest" not in item:
                raise ValueError("each file entry needs path and digest")
            items.append((item["path"], item["digest"]))
    else:
        raise ValueError("files must be a mapping or a list")

    files = {}
    for raw_path, digest in items:
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        files[path] = digest

    return {"version": version, "algorithm": algorithm, "files": files}


def diff_manifest(a, b):
    """Return the delta that turns manifest a into manifest b."""
    _, _, files_a = _manifest_parts(a)
    version_b, algorithm_b, files_b = _manifest_parts(b)

    add = {
        path: files_b[path]
        for path in sorted(files_b)
        if path not in files_a
    }
    update = {
        path: files_b[path]
        for path in sorted(files_b)
        if path in files_a and files_a[path] != files_b[path]
    }
    target_manifest = {
        "version": version_b,
        "algorithm": algorithm_b,
        "files": files_b,
    }
    return {
        "target_version": version_b,
        "algorithm": "sha256",
        "add": add,
        "update": update,
        "target_digest": _manifest_digest(target_manifest),
    }


def check_baseline(delta, current_version):
    """Return whether the delta can be applied to current_version."""
    if not isinstance(delta, Mapping):
        return False
    baseline = delta.get("base_version")
    if baseline is None:
        return True
    return baseline == current_version


def validate_delta(delta, manifest):
    """Return problem codes for a delta against the target manifest."""
    if not isinstance(delta, Mapping):
        return ["delta_invalid"]
    try:
        _, manifest_algorithm, _ = _manifest_parts(manifest)
    except ValueError:
        return ["manifest_invalid"]

    problems = []
    try:
        delta_algorithm = _check_algorithm(delta.get("algorithm"))
    except ValueError:
        delta_algorithm = None
    if delta_algorithm != manifest_algorithm:
        problems.append("algorithm_mismatch")
    return problems


def apply_delta(files, delta):
    """Apply a delta to a file table and return the updated file table."""
    if not isinstance(files, dict):
        raise ValueError("files must be a mapping")
    if not isinstance(delta, Mapping):
        raise ValueError("delta must be a mapping")

    for raw_path in delta.get("delete", []):
        files.pop(normalize_path(raw_path), None)

    for raw_path, digest in (delta.get("add") or {}).items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        files[path] = files.get(path, "") + digest

    for raw_path, digest in (delta.get("update") or {}).items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        files[path] = digest

    return files


def verify_result(files, target_manifest):
    """Return the paths whose presence differs from the target manifest."""
    current = _canonical_files(files)
    expected = target_manifest.get("files") or {}
    if len(current) != len(expected):
        return sorted(set(current) ^ set(expected))
    return []


def delta_batch(clients, delta, manifest):
    """Apply one delta to a group of clients and report each result."""
    if not isinstance(clients, Mapping):
        raise ValueError("clients must be a mapping")

    results = {}
    current = {}
    for client_id, client in clients.items():
        current.update(client.get("files") or {})
        applied = apply_delta(current, delta)
        mismatches = verify_result(applied, manifest)
        results[client_id] = {
            "ok": not mismatches,
            "baseline_ok": True,
            "files": applied,
            "problems": mismatches,
        }
    return results