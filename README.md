# Stringboard

Browse Firefox's UI strings (from [mozilla-firefox/firefox](https://github.com/mozilla-firefox/firefox))
with context, last-update/staleness info, and a one-click "View source" link
to the exact line on GitHub.

## How it works

- `pull_strings.py` fetches a curated list of `.ftl` files, parses them with
  `fluent.syntax`, and enriches each string with real commit metadata
  (`lastUpdated`, `lastAuthor`) via GitHub's GraphQL blame API.
- `stale` is computed: a string is stale if its last commit is older than
  `STALE_THRESHOLD_DAYS` (540 days / ~18 months, adjustable in the script).
- `voiceChecked` is a manual boolean a content designer sets after reviewing
  a string for voice consistency - not auto-detected.
- `trigger` / `visibility` are left as `null` for now - filling these in
  needs either manual tagging or a code-search pass.
- A GitHub Actions workflow (`.github/workflows/refresh.yml`) re-runs the
  pull nightly and commits any changes, so the data here stays current
  without anyone needing to run anything by hand.
- `index.html` is a static page (served via GitHub Pages) that reads
  `real_strings.json` directly - no backend.

## Running locally

```
pip install -r requirements.txt
gh auth login   # needed for the GraphQL blame calls
python3 pull_strings.py
python3 -m http.server 8000   # then open http://localhost:8000
```
