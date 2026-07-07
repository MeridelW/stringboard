"""
Stringboard - real data pull, stage 4 (resolved text + per-field rows)
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

Record shape (stage 4): each Fluent message becomes one record PER FIELD
(its main value, plus each attribute) rather than one record with attributes
bundled in a dict. This matters because a Fluent message's value and each of
its attributes (.label, .tooltiptext, .aria-label, ...) render in different
places in the UI and deserve independent review, not a single blob. Two
fields are handled specially:
  - accesskey / accessKey: dropped entirely. It's just the letter that gets
    underlined in a nearby label, wired-up internals rather than content a
    person reads.
  - aria-label: kept, but flagged (fieldLabel "Screen reader label") so a
    consumer can filter it out of an on-screen-copy-only view - it's real
    content, but a different category (assistive-tech only, never shown as
    its own visible text).
Android/iOS strings don't have Fluent attributes, so they always produce
exactly one record per string (field="value"), but still get the same
fieldLabel content-type guess so the column is meaningful on every platform.

Fluent placeables (variables, term/message references, select expressions
like `{ PLATFORM() -> ... }` for OS-specific wording, or `{ $count ->
[one] ... *[other] ... }` for plural forms) are fully resolved rather than
naively concatenated - the previous version of this script only joined each
TextElement's raw `.value` and silently dropped every Placeable, which left
real gaps (e.g. browser-main-private-window-title came out as an EMPTY
string, and private-browsing-shortcut-text-2 lost its leading "Firefox").
Each select expression's variants are preserved (not collapsed to a single
"general case"), because seeing what actually changes between e.g. macOS and
other platforms is exactly the point of a content audit.

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
from fluent.syntax import ast as fast

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

BRAND_FILE = {
    "owner": "mozilla-firefox",
    "repo": "firefox",
    "path": "browser/branding/official/locales/en-US/brand.ftl",
}

# A handful of partner/feature brand terms aren't defined in brand.ftl (they
# live in other components); these are well-known, unambiguous product/
# company names filled in directly rather than fetched.
EXTRA_TERMS = {
    "firefoxview-brand-name": "Firefox View",
    "firefox-suggest-brand-name": "Firefox Suggest",
    "yelp-brand-name": "Yelp",
    "mdn-brand-name": "MDN",
    "firefoxlabs-brand-name": "Firefox Labs",
}

# --- Friendly field labels for Fluent attributes (ground truth: the ---
# --- attribute name itself tells us what it is). ----------------------
FIELD_LABELS = {
    "label": "Label",
    "tooltiptext": "Tooltip",
    "aria-label": "Screen reader label",
    "aria-description": "Screen reader description",
    "title": "Title",
    "placeholder": "Placeholder text",
    "value": "Value",
    "message": "Status message",
    "toolbarname": "Toolbar name",
    "alt": "Alt text",
}
EXCLUDED_FIELDS = {"style"}  # CSS, not content

# Fields dropped entirely: never content on their own, just the letter that
# gets underlined in a nearby label. Matched by substring (not an exact set)
# because desktop uses several compound names for this across components -
# .accesskey, .accessKey, .secondarybuttonaccesskey,
# .buttonaccesskeyaccept, etc. - all of which are exactly the same kind of
# internal wiring under a different name.
HIDDEN_FIELD_SUBSTRING = "accesskey"

# Screen-reader-only fields: real content, but never shown as visible
# on-screen text - read aloud by assistive tech only. Kept in the data but
# flagged so a consumer can filter them out of an on-screen-copy-only view.
SCREEN_READER_FIELDS = {"aria-label", "aria-description"}

# --- Guessing a UI role for a string's main value ---------------------
# Fluent's own AST (and Android/iOS string resources) have no notion of
# "this becomes a button label" - that mapping only exists in the code that
# consumes the string. The ID naming convention is the only signal available
# from the source file alone, and it only clearly indicates a role for a
# minority of strings, so this is a best-effort guess (flagged as such via
# fieldLabelInferred), not ground truth like the attribute-name-derived
# labels above.
VALUE_ROLE_RULES = [
    (re.compile(r"(^|[-_.])buttons?([-_.]|$)", re.I), "Button label"),
    (re.compile(r"(^|[-_.])menu-?item(s)?([-_.]|$)", re.I), "Menu item"),
    (re.compile(r"(^|[-_.])checkbox([-_.]|$)", re.I), "Checkbox label"),
    (re.compile(r"(^|[-_.])radio([-_.]|$)", re.I), "Radio option label"),
    (re.compile(r"(^|[-_.])tooltip([-_.]|$)", re.I), "Tooltip"),
    (re.compile(r"(^|[-_.])placeholder([-_.]|$)", re.I), "Placeholder text"),
    (re.compile(r"(^|[-_.])link(s)?([-_.]|$)", re.I), "Link text"),
    (re.compile(r"(^|[-_.])(header|heading|headline|subheader)([-_.]|$)", re.I), "Heading"),
    (re.compile(r"(^|[-_.])title([-_.]|$)", re.I), "Title"),
    (re.compile(r"(^|[-_.])subtitle([-_.]|$)", re.I), "Subtitle"),
    (re.compile(r"(^|[-_.])description([-_.]|$)", re.I), "Description"),
    (re.compile(r"(^|[-_.])label(s)?([-_.]|$)", re.I), "Label"),
    (re.compile(r"(^|[-_.])warning([-_.]|$)", re.I), "Warning message"),
    (re.compile(r"(^|[-_.])error([-_.]|$)", re.I), "Error message"),
    (re.compile(r"(^|[-_.])notification([-_.]|$)", re.I), "Notification text"),
    (re.compile(r"(^|[-_.])message([-_.]|$)", re.I), "Message text"),
]


def guess_value_role(id_name):
    for pattern, label in VALUE_ROLE_RULES:
        if pattern.search(id_name):
            return label, True
    return "Text", False


# --- Friendly labels for select-expression variant keys ----------------
PLATFORM_KEY_LABELS = {
    "macos": "macOS",
    "windows": "Windows",
    "linux": "Linux",
    "android": "Android",
    "other": "Other",
}


def variant_key_label(key):
    if isinstance(key, fast.NumberLiteral):
        return key.value
    raw = key.name
    return PLATFORM_KEY_LABELS.get(raw, raw[:1].upper() + raw[1:])


def selector_description(selector):
    if isinstance(selector, fast.FunctionReference) and selector.id.name == "PLATFORM":
        return "Wording varies by operating system"
    if isinstance(selector, fast.VariableReference):
        return f"Wording varies depending on ${selector.id.name}"
    return "Wording varies"


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


def make_record(platform, owner, repo, path, line, rid, section, field, field_label,
                 field_label_inferred, variants, variant_note, note, blame_ranges,
                 screen_reader_only=False):
    last_updated, last_author = blame_for_line(blame_ranges, line)
    return {
        "platform": platform,
        "id": rid,
        "file": path,
        "line": line,
        "sourceUrl": f"{blob_base(owner, repo)}{path}#L{line}",
        "section": section,
        "field": field,
        "fieldLabel": field_label,
        "fieldLabelInferred": field_label_inferred,
        # Real content, but read aloud by assistive tech only - never shown
        # as its own visible text. Flagged so a consumer can filter it out
        # of an on-screen-copy-only view without hardcoding field names.
        "screenReaderOnly": screen_reader_only,
        "variants": variants,
        "variantNote": variant_note,
        "note": note,
        "lastUpdated": last_updated,
        "lastAuthor": last_author,
        "stale": compute_stale(last_updated),
        # TODO: needs manual tagging or a code-search pass
        "trigger": None,
        "visibility": None,
        # Set by hand by a content designer, not auto-detected:
        "voiceChecked": False,
    }


# ======================================================================
# Fluent (desktop) resolution
# ======================================================================

def load_terms():
    """Term dictionary (brand names etc.) resolved from the real brand.ftl,
    plus a few partner/feature terms that live outside it (see EXTRA_TERMS)."""
    terms = dict(EXTRA_TERMS)
    text = fetch(BRAND_FILE["owner"], BRAND_FILE["repo"], BRAND_FILE["path"])
    tree = fluent_parse(text, with_spans=False)
    for entry in tree.body:
        if isinstance(entry, fast.Term) and entry.value:
            terms[entry.id.name] = "".join(
                el.value for el in entry.value.elements if isinstance(el, fast.TextElement)
            )
    return terms


def resolve_pattern_flat(pattern, terms, message_index, depth=0):
    """Flat resolver: silently takes the default/general-case branch of any
    select expression. Used for a variant's own inner body and for message/
    term reference targets, where expanding every nested combination would
    be disproportionate."""
    if depth > 8 or pattern is None:
        return ""
    parts = []
    for el in pattern.elements:
        if isinstance(el, fast.TextElement):
            parts.append(el.value)
        elif isinstance(el, fast.Placeable):
            parts.append(resolve_expression_flat(el.expression, terms, message_index, depth))
    return "".join(parts)


def resolve_expression_flat(expr, terms, message_index, depth):
    if isinstance(expr, fast.StringLiteral):
        return expr.parse()["value"]
    if isinstance(expr, fast.NumberLiteral):
        return expr.value
    if isinstance(expr, fast.TermReference):
        return terms.get(expr.id.name, f"[{expr.id.name}]")
    if isinstance(expr, fast.MessageReference):
        target = message_index.get(expr.id.name)
        if target is not None and target.value and depth < 8:
            return resolve_pattern_flat(target.value, terms, message_index, depth + 1)
        return f"[{expr.id.name}]"
    if isinstance(expr, fast.VariableReference):
        return f"${expr.id.name}"
    if isinstance(expr, fast.FunctionReference):
        return f"${expr.id.name}(…)"
    if isinstance(expr, fast.SelectExpression):
        variant = next((v for v in expr.variants if v.default), expr.variants[-1])
        return resolve_pattern_flat(variant.value, terms, message_index, depth + 1)
    return ""


def resolve_variants(elements, idx, terms, message_index, depth=0):
    """Walks pattern elements left to right; hitting a select expression
    branches into one sub-result per variant (cross-multiplying with
    whatever follows, for the rare case of more than one select in a single
    pattern). Returns (variants, notes) where each variant is
    {"key": str|None, "isDefault": bool, "text": str}."""
    if idx >= len(elements):
        return [{"key": None, "isDefault": True, "text": ""}], []

    el = elements[idx]
    if isinstance(el, fast.TextElement):
        rest_variants, notes = resolve_variants(elements, idx + 1, terms, message_index, depth)
        return [
            {**v, "text": el.value + v["text"]} for v in rest_variants
        ], notes

    expr = el.expression
    if isinstance(expr, fast.SelectExpression) and depth < 8:
        rest_variants, rest_notes = resolve_variants(elements, idx + 1, terms, message_index, depth)
        notes = [selector_description(expr.selector)] + rest_notes
        branches = []
        for variant in expr.variants:
            key = variant_key_label(variant.key)
            own_text = resolve_pattern_flat(variant.value, terms, message_index, depth + 1)
            for rv in rest_variants:
                combined_key = f"{key} / {rv['key']}" if rv["key"] else key
                branches.append({
                    "key": combined_key,
                    "isDefault": bool(variant.default) and rv["isDefault"],
                    "text": own_text + rv["text"],
                })
        return branches, notes

    own_text = resolve_expression_flat(expr, terms, message_index, depth)
    rest_variants, notes = resolve_variants(elements, idx + 1, terms, message_index, depth)
    return [{**v, "text": own_text + v["text"]} for v in rest_variants], notes


def resolve_pattern_variants(pattern, terms, message_index):
    if pattern is None:
        return [{"key": None, "isDefault": True, "text": ""}], []
    variants, notes = resolve_variants(pattern.elements, 0, terms, message_index)
    for v in variants:
        v["text"] = re.sub(r"\s+", " ", v["text"]).strip()
    return variants, notes


VARS_HEADER_RE = re.compile(r"^\s*variables:\s*$", re.I)
VAR_LINE_RE = re.compile(r"^\s*\$(\w+)\s*(\([^)]*\))?\s*:?\s*(.*)$")


def parse_variable_descriptions(comment):
    if not comment:
        return {}
    lines = comment.split("\n")
    start = next((i for i, l in enumerate(lines) if VARS_HEADER_RE.match(l)), None)
    if start is None:
        return {}
    out = {}
    current = None
    for line in lines[start + 1:]:
        m = VAR_LINE_RE.match(line)
        if m:
            current = m.group(1)
            out[current] = m.group(3).strip()
        elif current and line.strip():
            out[current] = (out[current] + " " + line.strip()).strip()
    return out


SECTION_VARS_SPLIT_RE = re.compile(r"^\s*variables:\s*$", re.I | re.M)


def derive_section_label(content):
    """Section-comment paragraphs are often full sentences hard-wrapped
    across lines (not short headers): prefer the first full sentence, else
    truncate, rather than just taking the raw first line."""
    if not content:
        return None
    lines = content.split("\n")
    vars_idx = next((i for i, l in enumerate(lines) if VARS_HEADER_RE.match(l)), None)
    desc_lines = lines if vars_idx is None else lines[:vars_idx]
    text = re.sub(r"\s+", " ", " ".join(desc_lines)).strip()
    if not text:
        return None
    m = re.match(r"^(.{15,120}?[.!?])(\s|$)", text)
    if m:
        return m.group(1).rstrip(".!?")
    if len(text) > 120:
        text = re.sub(r"\s+\S*$", "", text[:120]) + "…"
    return text


def parse_fluent(platform, owner, repo, path, text, blame_ranges, terms):
    tree = fluent_parse(text, with_spans=True)
    message_index = {
        entry.id.name: entry
        for entry in tree.body
        if isinstance(entry, (fast.Message, fast.Term)) and entry.value
    }
    records = []
    current_section = None
    current_section_vars = {}

    for entry in tree.body:
        if isinstance(entry, (fast.GroupComment, fast.ResourceComment)):
            current_section = derive_section_label(entry.content)
            current_section_vars = parse_variable_descriptions(entry.content)
            continue
        if not isinstance(entry, (fast.Message, fast.Term)):
            continue

        id_name = ("-" if isinstance(entry, fast.Term) else "") + entry.id.name
        comment = entry.comment.content if entry.comment else None
        var_desc = {**current_section_vars, **parse_variable_descriptions(comment)}

        fields = []
        if entry.value:
            fields.append(("value", entry.value))
        for attr in entry.attributes:
            key = attr.id.name
            if key in EXCLUDED_FIELDS or HIDDEN_FIELD_SUBSTRING in key.lower():
                continue
            fields.append((attr.id.name, attr.value))

        for field_key, pattern in fields:
            variants_raw, notes = resolve_pattern_variants(pattern, terms, message_index)
            # Variable placeholders show as "$name" inline (see var_desc for
            # what they mean); not resolved further since they're filled in
            # at runtime.
            variants = [
                {"key": v["key"], "isDefault": v["isDefault"], "text": v["text"]}
                for v in variants_raw
            ]
            is_value = field_key == "value"
            if is_value:
                field_label, inferred = guess_value_role(id_name)
            else:
                field_label = FIELD_LABELS.get(field_key, field_key.replace("-", " ").title())
                inferred = False
            line = line_number(text, pattern.span.start)
            records.append(make_record(
                platform, owner, repo, path, line, id_name, current_section,
                "value" if is_value else f".{field_key}", field_label, inferred,
                variants, notes[0] if notes else None, comment, blame_ranges,
                screen_reader_only=field_key.lower() in SCREEN_READER_FIELDS,
            ))
    return records


# ======================================================================
# Android XML
# ======================================================================

ANDROID_STRING_RE = re.compile(
    r'(?:<!--\s*(?P<comment>(?:(?!-->).)*?)\s*-->\s*\n\s*)?'
    r'<string\s+name="(?P<name>[^"]+)"[^>]*>(?P<value>(?:(?!</string>).)*?)</string>',
    re.DOTALL,
)


def parse_android_xml(platform, owner, repo, path, text, blame_ranges, terms):
    records = []
    for m in ANDROID_STRING_RE.finditer(text):
        line = line_number(text, m.start())
        value = html.unescape(m.group("value").strip())
        field_label, inferred = guess_value_role(m.group("name"))
        records.append(make_record(
            platform, owner, repo, path, line, m.group("name"), None, "value",
            field_label, inferred, [{"key": None, "isDefault": True, "text": value}],
            None, m.group("comment"), blame_ranges,
        ))
    return records


# ======================================================================
# iOS .strings
# ======================================================================

IOS_STRING_RE = re.compile(
    r'(?:/\*\s*(?P<comment>(?:(?!\*/).)*?)\s*\*/\s*\n\s*)?'
    r'^"(?P<key>(?:[^"\\]|\\.)*)"\s*=\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*;',
    re.MULTILINE | re.DOTALL,
)


def parse_ios_strings(platform, owner, repo, path, text, blame_ranges, terms):
    records = []
    for m in IOS_STRING_RE.finditer(text):
        line = line_number(text, m.start())
        field_label, inferred = guess_value_role(m.group("key"))
        records.append(make_record(
            platform, owner, repo, path, line, m.group("key"), None, "value",
            field_label, inferred, [{"key": None, "isDefault": True, "text": m.group("value")}],
            None, m.group("comment"), blame_ranges,
        ))
    return records


PARSERS = {
    "fluent": parse_fluent,
    "android-xml": parse_android_xml,
    "ios-strings": parse_ios_strings,
}


def main():
    terms = load_terms()
    all_records = []
    for source in SOURCES:
        platform, owner, repo = source["platform"], source["owner"], source["repo"]
        parser = PARSERS[source["format"]]
        files = discover_files(source)
        print(f"\n[{platform}] discovered {len(files)} files in {owner}/{repo}")
        for path in files:
            text = fetch(owner, repo, path)
            blame_ranges = fetch_blame_ranges(owner, repo, path)
            records = parser(platform, owner, repo, path, text, blame_ranges, terms)
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
