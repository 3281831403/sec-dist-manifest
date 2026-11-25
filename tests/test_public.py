import hashlib
import json

from distman import (
    apply_delta,
    delta_batch,
    diff_manifest,
    parse_manifest,
)


def digest(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def manifest(version, files, algorithm="sha256"):
    return {"version": version, "algorithm": algorithm, "files": files}


def parse(version, files, algorithm="sha256"):
    return parse_manifest(json.dumps(manifest(version, files, algorithm)))


def test_parse_manifest_exposes_version_and_files():
    parsed = parse("3", {"assets/./icon.png": digest("icon")})

    assert parsed["version"] == "3"
    assert parsed["algorithm"] == "sha256"
    assert parsed["files"] == {"assets/icon.png": digest("icon")}


def test_diff_manifest_tracks_added_and_modified_files():
    before = parse(
        "1",
        {
            "keep.txt": digest("keep"),
            "docs/./guide.md": digest("guide-old"),
        },
    )
    after = parse(
        "2",
        {
            "keep.txt": digest("keep"),
            "docs/guide.md": digest("guide-new"),
            "new.txt": digest("new"),
        },
    )

    delta = diff_manifest(before, after)

    assert delta.get("add") == {"new.txt": digest("new")}
    assert delta.get("update") == {"docs/guide.md": digest("guide-new")}


def test_apply_delta_adds_file():
    before = parse("1", {})
    after = parse("2", {"media/./new.txt": digest("new")})

    delta = diff_manifest(before, after)
    result = apply_delta({}, delta)

    assert result.get("media/new.txt") == digest("new")
    assert result == {"media/new.txt": digest("new")}


def test_apply_delta_modifies_file():
    before = parse("1", {"docs/guide.md": digest("old")})
    after = parse("2", {"Docs/Guide.MD": digest("new")})

    delta = diff_manifest(before, after)
    result = apply_delta(before["files"], delta)

    assert result.get("docs/guide.md") == digest("new")
    assert result == {"docs/guide.md": digest("new")}


def test_delta_batch_keeps_clients_independent():
    before = parse("1", {"app.txt": digest("old")})
    after = parse("2", {"app.txt": digest("new")})
    delta = diff_manifest(before, after)
    clients = {
        "alpha": {"version": "1", "files": {"app.txt": digest("old")}},
        "beta": {"version": "0", "files": {"app.txt": digest("old")}},
    }

    results = delta_batch(clients, delta, after)

    assert results["alpha"]["ok"] is True
    assert results["alpha"]["files"] == {"app.txt": digest("new")}
    assert results["beta"]["ok"] is False
    assert results["beta"]["files"] == {"app.txt": digest("old")}