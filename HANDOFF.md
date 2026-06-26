# Stringboard - handoff to Claude Code

## What's proven (done in this chat session)
- `mozilla-firefox/firefox` on GitHub is the real, public, current source of
  truth for strings (confirms Option 1 from the brief is viable).
- Raw `.ftl` files are fetchable with no auth via raw.githubusercontent.com.
- `fluent.syntax` correctly parses real strings: group headers (## comments),
  values, attributes (.label, .accesskey, .title), and term references
  (e.g. `{ -brand-short-name }`).
- `pull_strings.py` does this for one real file (83 strings parsed
  successfully) and writes `real_strings.json` in the Stringboard shape.
- **View source**: each record now has a real `sourceUrl` linking straight
  to that string's line on GitHub (e.g. `.../permissions.ftl#L5`). No auth
  needed for this - it's just a URL built from the file path + computed
  line number. This should show as a "View source" link/icon on every row
  and in the detail panel.

## Status model (decided 2026-06-25)
- **stale**: computed, not hand-picked. A string is stale once its last
  commit is older than `STALE_THRESHOLD_DAYS` (currently set to 540 days /
  ~18 months in pull_strings.py). This is a real, adjustable constant -
  revisit it once real commit dates are flowing and you have a feel for
  what's actually too old. The mockup's "stale since 2017" style hardcoded
  labels go away in favor of this rule once `lastUpdated` is populated.
- **voiceChecked**: replaces the earlier "inconsistent voice, paired rows"
  concept from the mockup. It's a plain boolean (Yes/No) a content designer
  sets by hand after reviewing a string - voice consistency across surfaces
  needs human judgment, so this is a manual flag, not something we try to
  auto-detect from the repo. Defaults to `false` (not yet checked).

## What's blocked here, and why Claude Code is the next step
- The GitHub REST API (commits, code search) is rate-limited hard for
  unauthenticated/shared-IP requests. This blocks:
  - `lastUpdated` / `lastAuthor` (needs the commits API) - and therefore
    `stale`, since that's computed from `lastUpdated`
  - finding where a string ID is referenced in .js/.jsx (code search, to
    fill in "trigger" conditions for things like modals)
- Opening a real pull request needs push access to a fork tied to an
  actual GitHub account - not something an anonymous sandbox can do.
- All three of the above go away with your own `gh auth login` or a
  personal access token, which is normal in a Claude Code session running
  on your machine.

## Suggested first Claude Code session
1. `gh auth login` (or confirm git/GitHub credentials are already set up).
2. Either `git clone --depth 1 https://github.com/mozilla-firefox/firefox`
   (large repo - consider a sparse checkout of just the locale directories)
   or keep using raw fetches for content + the authenticated API for
   metadata, whichever is faster for the slice you're starting with.
3. Extend `pull_strings.py`:
   - Add more `.ftl` files (start with the ones covering your current
     work - persistent storage, password manager, site permissions).
   - For each string, call `GET /repos/.../commits?path=...` to get last
     commit date + author -> fills in `lastUpdated` / `lastAuthor`.
   - Compute `stale` from `lastUpdated` vs `STALE_THRESHOLD_DAYS`.
4. Leave `trigger` and `visibility` as null/TODO for now - those need
   either manual tagging or a code-search pass we'll do next.
5. Once we have a real JSON file with a meaningful number of strings, help me
   wire it into a simple local web UI (reuse the design from the mockup:
   sortable/filterable list, expandable rows, propose-an-edit box, and a
   "View source" link using `sourceUrl` on every row) so I can browse real
   Firefox strings instead of the mock data.

## Files attached
- `pull_strings.py` - the working fetch + parse script
- `real_strings.json` - its output, 83 real strings from permissions.ftl

