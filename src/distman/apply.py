"""Incremental distribution manifests: parse, diff, apply and verify.

A manifest is a JSON object with a version, a digest algorithm and a full
file table.  A delta names the baseline version it is built on, the target
version it produces, the file-level differences and the digest of the
complete target manifest.

Correctness rules implemented here:

* a manifest always lists every file together with its digest;
* a delta declares the baseline version it is based on;
* after applying a delta the table hashes to the declared target digest,
  using the algorithm named by the manifest;
* deleted files are listed explicitly;
* every path is normalised to a relative, case-folded, dot-segment-free key,
  so different spellings of the same path cannot appear twice;
* applying a delta is atomic -- the input table is never partially mutated;
* a batch applies the delta to an independent copy per client.

Only the Python standard library is used.  No function reads the filesystem,
the clock or any network resource.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

_DRIVE_RE = re.compile(r"^[a-z]:[\\/]")


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
    """Return the portable, canonical form of a relative path.

    Distribution manifests are written on several platforms, so native
    Windows separators are converted to forward slashes, ``.`` and ``..``
    segments are resolved, duplicate separators are collapsed and the whole
    path is case-folded.  Absolute paths, Windows drive prefixes and
    traversals that escape the distribution root are rejected.  Two spellings
    that normalise to the same key are therefore the same path.
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not path:
        raise ValueError("path must not be empty")

    replaced = path.replace("\\", "/")
    stripped = replaced.strip()
    if not stripped:
        raise ValueError("path must not be empty")
    if stripped.startswith("/"):
        raise ValueError("path must be relative: " + path)
    if _DRIVE_RE.match(stripped.lower()):
        raise ValueError("path must not contain a drive prefix: " + path)

    parts = []
    for segment in stripped.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise ValueError("path escapes the distribution root: " + path)
            parts.pop()
            continue
        parts.append(segment)

    if not parts:
        raise ValueError("path must not be empty")
    return "/".join(part.lower() for part in parts)


def _canonical_files(files):
    """Validate a file table and return a fresh copy keyed by normalised path.

    Two entries that normalise to the same path must carry the same digest;
    a digest conflict means the manifest (or table) is invalid.
    """
    if not isinstance(files, Mapping):
        raise ValueError("files must be a mapping")
    result = {}
    for raw_path, digest in files.items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        if path in result and result[path] != digest:
            raise ValueError("digest conflict for path: " + path)
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
    files = _canonical_files(manifest.get("files"))
    return version, algorithm, files


def _canonical_payload(version, algorithm, files):
    """Build the canonical byte string covered by the manifest digest."""
    lines = ["version=" + version, "algorithm=" + algorithm]
    for path in sorted(files):
        lines.append("file=" + path + "=" + files[path])
    return "\n".join(lines).encode("utf-8")


def _compute_digest(algorithm, payload):
    """Hash a byte string with the manifest-declared algorithm."""
    return hashlib.new(algorithm, payload).hexdigest()


def _manifest_digest(manifest):
    """Return the digest that identifies a complete manifest."""
    version, algorithm, files = _manifest_parts(manifest)
    return _compute_digest(
        algorithm, _canonical_payload(version, algorithm, files)
    )


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
        if path in files and files[path] != digest:
            raise ValueError("digest conflict for path: " + path)
        files[path] = digest

    return {"version": version, "algorithm": algorithm, "files": files}


def diff_manifest(a, b):
    """Return the delta that turns manifest a into manifest b.

    The delta carries the baseline and target versions, the declared digest
    algorithm, explicit add/update/delete sets and the digest of the complete
    target manifest.
    """
    version_a, algorithm_a, files_a = _manifest_parts(a)
    version_b, algorithm_b, files_b = _manifest_parts(b)
    if algorithm_a != algorithm_b:
        raise ValueError(
            "cannot diff manifests using different digest algorithms"
        )

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
    delete = sorted(path for path in files_a if path not in files_b)

    target_manifest = {
        "version": version_b,
        "algorithm": algorithm_b,
        "files": files_b,
    }
    return {
        "base_version": version_a,
        "target_version": version_b,
        "algorithm": algorithm_b,
        "add": add,
        "update": update,
        "delete": delete,
        "target_digest": _manifest_digest(target_manifest),
    }


def check_baseline(delta, current_version):
    """Return whether delta's declared baseline matches current_version.

    A delta without a baseline declaration cannot be trusted and is rejected.
    """
    if not isinstance(delta, Mapping):
        return False
    baseline = delta.get("base_version")
    if not isinstance(baseline, str) or not baseline:
        return False
    if not isinstance(current_version, str) or not current_version:
        return False
    return baseline == current_version


def _delta_parts(delta):
    """Validate a delta and return its normalised components.

    Raises ValueError when the delta is structurally unusable, including
    overlapping change sets and digest conflicts on a normalised path.
    """
    if not isinstance(delta, Mapping):
        raise ValueError("delta must be a mapping")

    base_version = delta.get("base_version")
    if not isinstance(base_version, str) or not base_version:
        raise ValueError("delta base_version must be a non-empty string")
    target_version = delta.get("target_version")
    if not isinstance(target_version, str) or not target_version:
        raise ValueError("delta target_version must be a non-empty string")
    algorithm = _check_algorithm(delta.get("algorithm"))

    target_digest = delta.get("target_digest")
    if not isinstance(target_digest, str) or not target_digest:
        raise ValueError("delta target_digest must be a non-empty string")

    add = {}
    for raw_path, digest in (delta.get("add") or {}).items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        if path in add and add[path] != digest:
            raise ValueError("digest conflict for path: " + path)
        add[path] = digest

    update = {}
    for raw_path, digest in (delta.get("update") or {}).items():
        path = normalize_path(raw_path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("digest must be a non-empty string")
        if path in update and update[path] != digest:
            raise ValueError("digest conflict for path: " + path)
        update[path] = digest

    delete = []
    seen_deletes = set()
    for raw_path in delta.get("delete") or []:
        path = normalize_path(raw_path)
        if path not in seen_deletes:
            seen_deletes.add(path)
            delete.append(path)

    overlap = (set(add) | set(update)) & set(delete)
    if overlap:
        raise ValueError(
            "path listed in both a change set and the delete set: "
            + ", ".join(sorted(overlap))
        )
    overlap = set(add) & set(update)
    if overlap:
        raise ValueError(
            "path listed in both add and update sets: "
            + ", ".join(sorted(overlap))
        )

    return {
        "base_version": base_version,
        "target_version": target_version,
        "algorithm": algorithm,
        "add": add,
        "update": update,
        "delete": delete,
        "target_digest": target_digest.lower(),
    }


def validate_delta(delta, manifest):
    """Return problem codes for a delta against its target manifest.

    An empty list means the delta is consistent with the manifest.  Problem
    codes are stable strings so callers can branch on them.
    """
    if not isinstance(delta, Mapping):
        return ["delta_invalid"]
    try:
        _, manifest_algorithm, manifest_files = _manifest_parts(manifest)
    except ValueError:
        return ["manifest_invalid"]

    try:
        parts = _delta_parts(delta)
    except ValueError:
        return ["delta_invalid"]

    problems = []
    if parts["algorithm"] != manifest_algorithm:
        problems.append("algorithm_mismatch")
    if parts["target_version"] != manifest.get("version"):
        problems.append("target_version_mismatch")

    # The change sets must agree with the *target* manifest: every added or
    # updated path must exist in the target with the same digest, and no
    # deleted path may still be present.
    expected = {**parts["add"], **parts["update"]}
    for path, digest in expected.items():
        if path not in manifest_files:
            problems.append("path_missing_from_target")
        elif manifest_files[path] != digest:
            problems.append("digest_mismatch")
    for path in parts["delete"]:
        if path in manifest_files:
            problems.append("delete_path_exists")

    if parts["target_digest"] != _manifest_digest(manifest).lower():
        problems.append("target_digest_mismatch")
    return problems


def apply_delta(files, delta):
    """Apply a delta to a file table and return the resulting table.

    The application is atomic: the delta is fully validated and the result
    fully verified before anything is returned, and the input mapping is
    never mutated.  Re-applying the same delta to the state it produced
    returns the same table.
    """
    if not isinstance(files, Mapping):
        raise ValueError("files must be a mapping")

    parts = _delta_parts(delta)

    # Work on an independent, normalised copy so the caller's table is
    # untouched on every outcome.
    current = _canonical_files(files)

    for path in parts["delete"]:
        current.pop(path, None)
    for path, digest in parts["add"].items():
        current[path] = digest
    for path, digest in parts["update"].items():
        current[path] = digest

    actual_digest = _compute_digest(
        parts["algorithm"],
        _canonical_payload(
            parts["target_version"], parts["algorithm"], current
        ),
    )
    if actual_digest.lower() != parts["target_digest"]:
        raise ValueError(
            "resulting manifest digest does not match the target digest"
        )
    return current


def verify_result(files, target_manifest):
    """Return the normalised paths whose state differs from the target.

    A path is reported when it is missing, unexpectedly present, or present
    with a digest different from the target manifest's digest.  The returned
    list is sorted and empty when the table exactly matches the target.
    """
    try:
        current = _canonical_files(files)
        _, _, expected = _manifest_parts(target_manifest)
    except ValueError:
        try:
            return sorted(_canonical_files(files))
        except ValueError:
            return []

    differences = set()
    for path, digest in current.items():
        if path not in expected or expected[path] != digest:
            differences.add(path)
    for path in expected:
        if path not in current:
            differences.add(path)
    return sorted(differences)


def delta_batch(clients, delta, manifest):
    """Apply one delta to a group of clients, independently per client.

    Each client record carries its own ``version`` and ``files``.  Every
    result contains ``ok``, ``baseline_ok``, an independent ``files`` table
    (the client's own table, left untouched on failure) and ``problems``.
    One client's state can never leak into another client's result.
    """
    if not isinstance(clients, Mapping):
        raise ValueError("clients must be a mapping")

    try:
        _manifest_parts(manifest)
        manifest_valid = True
    except ValueError:
        manifest_valid = False

    try:
        parts = _delta_parts(delta)
        delta_valid = True
    except ValueError:
        parts, delta_valid = None, False

    results = {}
    for client_id, client in clients.items():
        if not isinstance(client, Mapping):
            results[client_id] = {
                "ok": False,
                "baseline_ok": False,
                "files": {},
                "problems": ["client_invalid"],
            }
            continue

        # Independent normalised copy: no aliasing between clients or with
        # the caller's input tables.
        try:
            own_files = _canonical_files(client.get("files") or {})
        except ValueError:
            results[client_id] = {
                "ok": False,
                "baseline_ok": False,
                "files": dict(client.get("files") or {}),
                "problems": ["client_invalid"],
            }
            continue

        current_version = client.get("version")
        baseline_ok = (
            delta_valid
            and isinstance(current_version, str)
            and current_version == parts["base_version"]
        )

        problems = []
        applied = None
        if not manifest_valid:
            problems.append("manifest_invalid")
        if not delta_valid:
            problems.append("delta_invalid")
        elif not baseline_ok:
            problems.append("baseline_mismatch")
        elif manifest_valid:
            try:
                applied = apply_delta(own_files, delta)
            except ValueError:
                problems.append("apply_failed")

        if applied is not None:
            mismatches = verify_result(applied, manifest)
            if mismatches:
                problems.extend(mismatches)

        # De-duplicate while preserving a stable order.
        seen = set()
        unique_problems = []
        for problem in problems:
            if problem not in seen:
                seen.add(problem)
                unique_problems.append(problem)

        results[client_id] = {
            "ok": not unique_problems,
            "baseline_ok": baseline_ok,
            "files": applied if applied is not None else own_files,
            "problems": unique_problems,
        }
    return results
