"""
Stringboard - real data pull, stage 2
Pulls live .ftl files from the mozilla-firefox/firefox repo on GitHub,
parses them into the Stringboard record shape, and enriches each string
with real last-commit metadata (date + author) via the GitHub GraphQL
blame API.

What this does:
  - Fetches .ftl files via raw.githubusercontent.com (no auth needed).
  - Parses them with fluent.syntax: group headers (## comments), values,
    attributes (.label, .accesskey, .title), term references.
  - Builds a real, working "View source" sourceUrl per string.
  - For each file, runs one GraphQL "blame" query (via `gh api graphql`,
    using the token from `gh auth login`) to get the line-level commit
    history, then maps each string's line number to the commit that last
    touched it. This gives accurate per-string lastUpdated/lastAuthor
    with one API call per file, instead of one REST call per string.
  - Computes "stale" from lastUpdated vs STALE_THRESHOLD_DAYS.

Status model (decided 2026-06-25):
  - "stale" is computed: a string is stale if its last commit is older
    than STALE_THRESHOLD_DAYS (default 540 days / ~18 months). Adjust
    once real commit data is flowing and you have a feel for what's
    actually too old.
  - "voiceChecked" is a plain boolean a content designer sets by hand
    after reviewing a string - not auto-detected. Defaults to False.

Still TODO (needs manual tagging or a code-search pass, not done here):
  - "trigger" - what UI action surfaces this string
  - "visibility" - which builds/channels/platforms show it
  - opening a real pull request

Requires:
  - `gh auth login` already done (this script shells out to `gh api graphql`)

Usage:
  python3 pull_strings.py
"""
import json
import subprocess
import urllib.request
from datetime import datetime, timezone

from fluent.syntax import parse
from fluent.syntax.ast import Message, GroupComment

RAW_BASE = "https://raw.githubusercontent.com/mozilla-firefox/firefox/main/"
BLOB_BASE = "https://github.com/mozilla-firefox/firefox/blob/main/"
OWNER = "mozilla-firefox"
REPO = "firefox"
STALE_THRESHOLD_DAYS = 540  # ~18 months - adjust once real dates are in

# Real Firefox locale files, picked for areas Meridel is actively working
# in (permissions, password import/migration, password autofill). Add
# more here as we cover more surfaces.
FILES = [
    "browser/locales/en-US/browser/preferences/permissions.ftl",
    "browser/locales/en-US/browser/browser.ftl",
    "browser/locales/en-US/browser/permissions.ftl",  # persistent-storage doorhanger
    "browser/locales/en-US/browser/migrationWizard.ftl",  # password import/migration dialog
    "toolkit/locales/en-US/toolkit/main-window/autocomplete.ftl",  # password autofill suggestions
    "browser/locales/en-US/browser/aboutLogins.ftl",  # password manager page
]

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
                author {
                  name
                  user { login }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def fetch(path):
    url = RAW_BASE + path
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def line_number(text, offset):
    return text.count("\n", 0, offset) + 1


def fetch_blame_ranges(path):
    """One GraphQL call per file: returns a list of
    (starting_line, ending_line, committed_date, author_name) tuples."""
    result = subprocess.run(
        [
            "gh", "api", "graphql",
            "-f", f"query={BLAME_QUERY}",
            "-f", f"owner={OWNER}",
            "-f", f"name={REPO}",
            "-f", f"path={path}",
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    ranges = data["data"]["repository"]["ref"]["target"]["blame"]["ranges"]
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


def parse_file(path, text, blame_ranges):
    tree = parse(text, with_spans=True)
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
            last_updated, last_author = blame_for_line(blame_ranges, line)
            records.append({
                "id": entry.id.name,
                "file": path,
                "line": line,
                "sourceUrl": f"{BLOB_BASE}{path}#L{line}",
                "group": current_group,
                "value": value,
                "attributes": attrs,
                "lastUpdated": last_updated,
                "lastAuthor": last_author,
                "stale": compute_stale(last_updated),
                # TODO: needs manual tagging or a code-search pass
                "trigger": None,
                "visibility": None,
                # Set by hand by a content designer, not auto-detected:
                "voiceChecked": False,
            })
    return records


def main():
    all_records = []
    for path in FILES:
        text = fetch(path)
        blame_ranges = fetch_blame_ranges(path)
        records = parse_file(path, text, blame_ranges)
        all_records.extend(records)
        print(f"{path}: {len(records)} strings parsed")

    with open("real_strings.json", "w") as f:
        json.dump(all_records, f, indent=2)

    print(f"\nTotal: {len(all_records)} strings written to real_strings.json")
    print("\nSample record:")
    print(json.dumps(all_records[0], indent=2))


if __name__ == "__main__":
    main()
