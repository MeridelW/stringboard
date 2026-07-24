"""
One-off regeneration of real_strings.json using the rewritten pull_strings.py
parsing logic, WITHOUT calling the GitHub API for file discovery or blame.

Why this exists (not part of the normal pipeline): this sandbox's GitHub
access is scoped to only the stringboard repo itself - calls to
mozilla-firefox/firefox or mozilla-mobile/firefox-ios via `gh api` (used by
discover_files() and fetch_blame_ranges() in pull_strings.py) return 403.
Raw file content IS fetchable (raw.githubusercontent.com is a plain CDN, not
the API), so this script:
  1. Reuses the file list already present in the CURRENT real_strings.json
     (already discovered by a prior authenticated run) instead of calling
     discover_files().
  2. Reuses each existing record's lastUpdated/lastAuthor/stale, keyed by
     (platform, id, file), instead of calling fetch_blame_ranges(). Since
     the old schema had one record per message (not per field), every new
     field-row split out of that message inherits the SAME blame data as
     an approximation - it's exactly right for messages with only one
     field, and a same-commit-is-likely approximation for split attribute
     rows until the next real, authenticated nightly refresh recomputes
     precise per-line blame (pull_strings.py itself computes this
     correctly when it has real `gh auth`).

Usage:
  python3 regenerate_local.py
"""
import json

import pull_strings as ps

OLD_PATH = "real_strings.json"


def load_old_blame_and_files():
    with open(OLD_PATH) as f:
        old = json.load(f)
    blame = {}
    files_by_platform = {}
    for r in old:
        key = (r["platform"], r["id"], r["file"])
        # Keep the first blame seen per key (old schema had exactly one
        # record per key, so this is just a straightforward lookup).
        blame.setdefault(key, (r["lastUpdated"], r["lastAuthor"], r["stale"]))
        files_by_platform.setdefault(r["platform"], set()).add(r["file"])
    return blame, {p: sorted(files) for p, files in files_by_platform.items()}


def main():
    old_blame, files_by_platform = load_old_blame_and_files()
    terms = ps.load_terms()
    print(f"Loaded {len(terms)} brand terms.")

    all_records = []
    fetch_failures = []

    for source in ps.SOURCES:
        platform, owner, repo = source["platform"], source["owner"], source["repo"]
        parser = ps.PARSERS[source["format"]]
        files = files_by_platform.get(platform, [])
        print(f"\n[{platform}] {len(files)} files (from existing real_strings.json)")

        for path in files:
            try:
                text = ps.fetch(owner, repo, path)
            except Exception as e:
                fetch_failures.append((path, str(e)))
                print(f"  FAILED to fetch {path}: {e}")
                continue

            # blame_ranges normally comes from one GraphQL call per file;
            # here we instead look up each resulting record's blame from the
            # OLD data, so pass a fake blame lookup surface via monkeypatch
            # per-file below rather than real ranges.
            records = parser(platform, owner, repo, path, text, [], terms)

            for r in records:
                key = (platform, r["id"], path)
                last_updated, last_author, stale = old_blame.get(key, (None, None, None))
                r["lastUpdated"] = last_updated
                r["lastAuthor"] = last_author
                r["stale"] = stale

            all_records.extend(records)
            print(f"  {path}: {len(records)} rows")

    with open(OLD_PATH, "w") as f:
        json.dump(all_records, f, indent=2)

    by_platform = {}
    for r in all_records:
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1
    print(f"\nTotal: {len(all_records)} rows written to {OLD_PATH}")
    print("By platform:", by_platform)
    if fetch_failures:
        print(f"\n{len(fetch_failures)} file(s) failed to fetch:")
        for path, err in fetch_failures:
            print(f"  {path}: {err}")


if __name__ == "__main__":
    main()
