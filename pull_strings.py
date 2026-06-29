"""
Stringboard - real data pull, stage 3 (multi-platform)
Pulls live source strings for Firefox Desktop, Android, and iOS and parses
them into a single, common Stringboard record shape, enriched with real
commit metadata.

Sources (auto-discovered, not hand-picked, so adding new files upstream
doesn't require touching this script):
  - Desktop: all .ftl files under browser/locales/en-US and
    toolkit/locales/en-US in mozilla-firefox/firefox (Fluent format).
  - Android: all base (English-source) strings.xml files under
    mobile/android/fenix and mobile/android/android-components in
    mozilla-firefox/firefox (Android XML format), excluding
    samples/examples/test-only modules and Focus (a different product).
  - iOS: Localizable.strings files under firefox-ios/firefox-ios in the
    separate mozilla-mobile/firefox-ios repo (Apple .strings format).

For each file, one GraphQL "blame" query gets line-level commit history,
which is mapped to each string's line number for real lastUpdated/lastAuthor
- one API call per file rather than one per string.

Status model (decided 2026-06-25):
  - "stale": computed - last commit older than STALE_THRESHOLD_DAYS
    (540 days / ~18 months, adjustable below).
  - "voiceChecked": plain boolean a content designer sets by hand after
    review - not auto-detected. Defaults to False.

Still TODO (needs manual tagging or a code-search pass, not done here):
  - "trigger" - what UI action surfaces this string
  - "visibility" - which builds/channels/platforms show it
  - opening a real pull request

Requires:
  - `gh auth login` already done (this script shells out to `gh api graphql`
    and uses `gh auth token` for direct REST/tree calls).

Usage:
  python3 pull_strings.py
"""
import html
import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone

from fluent.syntax import parse as fluent_parse
from fluent.syntax.ast import Message, GroupComment

STALE_THRESHOLD_DAYS = 540  # ~18 months - adjust once real dates are in

BLAME_QUERY = """
query($owner: String!, $name: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: "main") {
      target {
        ... on Commit {
          blame(path: $path) {
            ranges {
              startingLine
              endingLine
              commit {
                committedDate
                author { name user { login } }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Each source describes a repo + discovery roots + which parser to use.
SOURCES = [
    {
        "platform": "desktop",
        "owner": "mozilla-firefox",
        "repo": "firefox",
        "format": "fluent",
        "roots": [
            {"path": "browser/locales/en-US", "ext": ".ftl"},
            {"path": "toolkit/locales/en-US", "ext": ".ftl"},
        ],
    },
    {
        "platform": "android",
        "owner": "mozilla-firefox",
        "repo": "firefox",
        "format": "android-xml",
        "roots": [
            {"path": "mobile/android/fenix/app/src/main/res/values", "filename": "strings.xml"},
            {"path": "mobile/android/android-components/components", "filename": "strings.xml", "must_contain": "/values/"},
        ],
        "exclude_substrings": ["/samples/", "/examples/", "fenix/app/longfox"],
    },
    {
        "platform": "ios",
        "owner": "mozilla-mobile",
        "repo": "firefox-ios",
        "format": "ios-strings",
        "roots": [
            {"path": "firefox-ios", "filename": "Localizable.strings", "must_contain": "en-US.lproj"},
        ],
    },
]


def raw_base(owner, repo):
    return f"https://raw.githubusercontent.com/{owner}/{repo}/main/"


def blob_base(owner, repo):
    return f"https://github.com/{owner}/{repo}/blob/main/"


def fetch(owner, repo, path):
    url = raw_base(owner, repo) + path
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def line_number(text, offset):
    return text.count("\n", 0, offset) + 1


def gh_api(path_with_query):
    result = subprocess.run(
        ["gh", "api", path_with_query],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def resolve_subtree_sha(owner, repo, path):
    """Walk from the repo root tree to the tree sha for `path`."""
    sha = "main"
    for part in path.split("/"):
        data = gh_api(f"repos/{owner}/{repo}/git/trees/{sha}")
        match = next((t for t in data["tree"] if t["path"] == part), None)
        if not match:
            raise RuntimeError(f"path component {part!r} not found while resolving {path}")
        sha = match["sha"]
    return sha


def discover_files(source):
    owner, repo = source["owner"], source["repo"]
    excludes = source.get("exclude_substrings", [])
    found = []
    for root in source["roots"]:
        sha = resolve_subtree_sha(owner, repo, root["path"])
        data = gh_api(f"repos/{owner}/{repo}/git/trees/{sha}?recursive=1")
        if data.get("truncated"):
            print(f"WARNING: tree listing truncated for {owner}/{repo}:{root['path']}")
        for entry in data["tree"]:
            if entry.get("type") != "blob":
                continue
            full_path = f"{root['path']}/{entry['path']}"
            if "ext" in root and not full_path.endswith(root["ext"]):
                continue
            if "filename" in root and not full_path.endswith("/" + root["filename"]):
                continue
            if "must_contain" in root and root["must_contain"] not in full_path:
                continue
            if any(ex in full_path for ex in excludes):
                continue
            found.append(full_path)
    return sorted(set(found))


def fetch_blame_ranges(owner, repo, path):
    """One GraphQL call per file: list of
    (starting_line, ending_line, committed_date, author_name)."""
    result = subprocess.run(
        [
            "gh", "api", "graphql",
            "-f", f"query={BLAME_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={repo}",
            "-f", f"path={path}",
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    target = data["data"]["repository"]["ref"]["target"]
    if not target:
        return []
    ranges = target["blame"]["ranges"]
    out = []
    for r in ranges:
        commit = r["commit"]
        author = commit["author"]
        author_name = (author.get("user") or {}).get("login") or author.get("name")
        out.append((r["startingLine"], r["endingLine"], commit["committedDate"], author_name))
    return out


def blame_for_line(ranges, line):
    for start, end, date, author in ranges:
        if start <= line <= end:
            return date, author
    return None, None


def compute_stale(last_updated_iso):
    if not last_updated_iso:
        return None
    last_updated = datetime.fromisoformat(last_updated_iso.replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - last_updated).days
    return age_days > STALE_THRESHOLD_DAYS


def make_record(platform, owner, repo, path, line, rid, group, value, attributes, note, blame_ranges):
    last_updated, last_author = blame_for_line(blame_ranges, line)
    return {
        "platform": platform,
        "id": rid,
        "file": path,
        "line": line,
        "sourceUrl": f"{blob_base(owner, repo)}{path}#L{line}",
        "group": group,
        "note": note,
        "value": value,
        "attributes": attributes,
        "lastUpdated": last_updated,
        "lastAuthor": last_author,
        "stale": compute_stale(last_updated),
        # TODO: needs manual tagging or a code-search pass
        "trigger": None,
        "visibility": None,
        # Set by hand by a content designer, not auto-detected:
        "voiceChecked": False,
    }


def parse_fluent(platform, owner, repo, path, text, blame_ranges):
    tree = fluent_parse(text, with_spans=True)
    records = []
    current_group = None
    for entry in tree.body:
        if isinstance(entry, GroupComment):
            current_group = entry.content.strip()
            continue
        if isinstance(entry, Message):
            value = None
            if entry.value:
                value = "".join(
                    el.value for el in entry.value.elements if hasattr(el, "value")
                )
            attrs = {}
            for attr in entry.attributes:
                attrs[attr.id.name] = "".join(
                    el.value for el in attr.value.elements if hasattr(el, "value")
                )
            line = line_number(text, entry.span.start)
            records.append(make_record(
                platform, owner, repo, path, line, entry.id.name,
                current_group, value, attrs, None, blame_ranges,
            ))
    return records


ANDROID_STRING_RE = re.compile(
    r'(?:<!--\s*(?P<comment>(?:(?!-->).)*?)\s*-->\s*\n\s*)?'
    r'<string\s+name="(?P<name>[^"]+)"[^>]*>(?P<value>(?:(?!</string>).)*?)</string>',
    re.DOTALL,
)


def parse_android_xml(platform, owner, repo, path, text, blame_ranges):
    records = []
    for m in ANDROID_STRING_RE.finditer(text):
        line = line_number(text, m.start())
        value = html.unescape(m.group("value").strip())
        records.append(make_record(
            platform, owner, repo, path, line, m.group("name"),
            None, value, {}, m.group("comment"), blame_ranges,
        ))
    return records


IOS_STRING_RE = re.compile(
    r'(?:/\*\s*(?P<comment>(?:(?!\*/).)*?)\s*\*/\s*\n\s*)?'
    r'^"(?P<key>(?:[^"\\]|\\.)*)"\s*=\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*;',
    re.MULTILINE | re.DOTALL,
)


def parse_ios_strings(platform, owner, repo, path, text, blame_ranges):
    records = []
    for m in IOS_STRING_RE.finditer(text):
        line = line_number(text, m.start())
        records.append(make_record(
            platform, owner, repo, path, line, m.group("key"),
            None, m.group("value"), {}, m.group("comment"), blame_ranges,
        ))
    return records


PARSERS = {
    "fluent": parse_fluent,
    "android-xml": parse_android_xml,
    "ios-strings": parse_ios_strings,
}


def main():
    all_records = []
    for source in SOURCES:
        platform, owner, repo = source["platform"], source["owner"], source["repo"]
        parser = PARSERS[source["format"]]
        files = discover_files(source)
        print(f"\n[{platform}] discovered {len(files)} files in {owner}/{repo}")
        for path in files:
            text = fetch(owner, repo, path)
            blame_ranges = fetch_blame_ranges(owner, repo, path)
            records = parser(platform, owner, repo, path, text, blame_ranges)
            all_records.extend(records)
            print(f"  {path}: {len(records)} strings")

    with open("real_strings.json", "w") as f:
        json.dump(all_records, f, indent=2)

    by_platform = {}
    for r in all_records:
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1
    print(f"\nTotal: {len(all_records)} strings written to real_strings.json")
    print("By platform:", by_platform)


if __name__ == "__main__":
    main()
