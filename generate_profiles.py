#!/usr/bin/env python3
"""
Schema-driven synthetic data generator.

Give it any CSV. It reads the columns, works out what kind of data each one
holds (names, dates, numbers, categories, URLs, free text, nested JSON), and
generates a fake dataset with the same columns, the same nested structure and
roughly the same fill rates. Nothing in the code is tied to one dataset.

USAGE
    1. Install Faker once:
           pip install faker

    2. Run it on a CSV:
           python generate_profiles.py LinkedIn_people_profiles_datasets.csv

       This writes two files in the folder you ran it from:
           LinkedIn_people_profiles_datasets_synthetic.json
           LinkedIn_people_profiles_datasets_synthetic.csv

    3. The exact command for the Group 3 dataset:
           python generate_profiles.py LinkedIn_people_profiles_datasets.csv --n 400 --seed 6102 --out profiles_synthetic

OPTIONS
    --n 400            how many fake rows to generate              (default 400)
    --seed 6102        same seed = identical output every time     (default 6102)
    --out NAME         output file name, no extension              (default <input>_synthetic)
    --show-schema      print what the script decided each column is
    --min-count 5      how many rows must share a word or value
                       before it may be reused                     (default 5)


PRIVACY RULES (why no real person appears in the output)
    "k" below is --min-count, default 5.

    * Names, emails, phone numbers, URLs and ids are always generated.
    * Categories (country codes, industries, regions) reuse only values that
      at least k different source rows share.
    * Short text keeps the SHAPE of a real value. Each word is kept only if
      at least k different rows contain it ("Senior", "Analyst", "at",
      "years"); rarer words, which is where personal names and small
      employers live, are replaced by fake ones. A consequence: a generic
      phrase made only of shared words, such as "Director of Finance &
      Corporate Controller", can come out identical to a real value. That
      is expected; by construction it contains no rare word.
    * Long text (descriptions, about sections) is never copied at all; Faker
      writes new text of a similar length.

    Checked on the LinkedIn people file: none of the 919 real first+last
    name pairs appears anywhere in the output, nested fields included.

OUTPUT
    <input>_synthetic.json   JSON array, nested fields as real JSON
    <input>_synthetic.csv    same columns as the input, nested fields as JSON text
"""

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from faker import Faker

csv.field_size_limit(sys.maxsize)

# Cell values treated as missing. "NA" is deliberately NOT here: in this data
# it means North America, and treating it as null silently drops real values.
NULLS = {"", "null", "None", "NULL", "nan", "NaN"}

LONG_TEXT = 100  # average characters above which text is regenerated, not mimicked

URL_RE = re.compile(r"^https?://", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
TOKEN_RE = re.compile(r"\w+|\W+")
COMPANY_MARKERS = re.compile(
    r"\b(inc|llc|ltd|gmbh|corp|corporation|group|company|co\.|s\.a\.|plc|bv|ag)\b", re.I)

# Country code -> Faker locale, so a Brazilian row gets a Brazilian name.
# Only Latin-script locales, so generated ids stay readable ASCII.
LOCALES = {
    "US": "en_US", "CA": "en_CA", "GB": "en_GB", "UK": "en_GB", "IE": "en_IE",
    "AU": "en_AU", "NZ": "en_NZ", "IN": "en_IN", "DE": "de_DE", "AT": "de_AT",
    "CH": "de_CH", "NL": "nl_NL", "BE": "nl_BE", "FR": "fr_FR", "ES": "es_ES",
    "MX": "es_MX", "AR": "es_AR", "CO": "es_CO", "CL": "es_CL", "IT": "it_IT",
    "PT": "pt_PT", "BR": "pt_BR", "SE": "sv_SE", "DK": "da_DK", "NO": "no_NO",
    "FI": "fi_FI", "PL": "pl_PL", "CZ": "cs_CZ", "RO": "ro_RO", "TR": "tr_TR",
}


# ---------------------------------------------------------------------------
# 1. Learning the structure
# ---------------------------------------------------------------------------
class Node:
    """Everything learned about one field, at any nesting depth."""

    def __init__(self, path):
        self.path = path              # e.g. ("experience", "positions", "title")
        self.seen = 0                 # times the field slot existed
        self.present = 0              # times it held a non-null value
        self.kinds = Counter()        # scalar / list / dict
        self.samples = []             # (value, source_row) for scalars
        self.list_lengths = []
        self.items = None             # Node describing list elements
        self.keys = {}                # key -> Node, for objects
        self.dict_count = 0           # how many objects were observed here

    def observe(self, value, row):
        self.seen += 1
        if value is None or (isinstance(value, str) and value.strip() in NULLS):
            return
        self.present += 1
        if isinstance(value, list):
            self.kinds["list"] += 1
            self.list_lengths.append(len(value))
            if self.items is None:
                self.items = Node(self.path)
            for v in value:
                self.items.observe(v, row)
        elif isinstance(value, dict):
            self.kinds["dict"] += 1
            self.dict_count += 1
            for k, v in value.items():
                self.keys.setdefault(k, Node(self.path + (k,))).observe(v, row)
        else:
            self.kinds["scalar"] += 1
            self.samples.append((value, row))

    @property
    def fill(self):
        return self.present / self.seen if self.seen else 0.0


def parse_cell(raw):
    """CSV cell -> Python value. Cells holding JSON become lists/dicts."""
    if raw is None or raw.strip() in NULLS:
        return None
    s = raw.strip()
    if s[0] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return raw


def classify(node, k):
    """Decide how to generate a scalar field from its name and its values."""
    values = [v for v, _ in node.samples]
    if not values:
        return "empty"
    strs = [str(v) for v in values[:500]]
    path = ".".join(node.path).lower()
    last = node.path[-1].lower() if node.path else ""

    def share(pred):
        return sum(1 for v in strs if pred(v)) / len(strs)

    if all(isinstance(v, bool) for v in values[:500]):
        return "bool"
    if share(URL_RE.match) > 0.5:
        return "url"
    if "email" in last:
        return "email"
    if "phone" in last:
        return "phone"
    if re.search(r"(^|[_:.])name$", last) or last in ("fullname", "full_name"):
        if "compan" in path or "organi" in path or "employer" in path \
                or share(COMPANY_MARKERS.search) > 0.1:
            return "company"
        if "school" in path or "universit" in path:
            return "school"
        return "person"
    if last in ("company", "employer", "organization"):
        return "company"
    if last == "id" or last.endswith("_id") or last.endswith(":id"):
        return "id"
    if share(DATE_RE.match) > 0.9:
        return "date"
    if share(INT_RE.match) > 0.9:
        return "int"
    if share(lambda v: FLOAT_RE.match(v) or INT_RE.match(v)) > 0.9:
        return "float"
    # Category: most values are ones shared by at least k different rows.
    common_hits = sum(1 for v, _ in node.samples if str(v) in node.common_values)
    if node.common_values and common_hits / len(node.samples) > 0.6:
        return "category"
    return "text"


def finalize(node, k, row_cc):
    """Walk the tree, classify every node, and precompute what generation needs."""
    node.shape = node.kinds.most_common(1)[0][0] if node.kinds else "scalar"
    if node.shape == "list" and node.items is not None:
        finalize(node.items, k, row_cc)
        node.kind = "list"
        return
    if node.shape == "dict":
        for child in node.keys.values():
            finalize(child, k, row_cc)
        node.kind = "dict"
        return

    # Which whole values, and which words, are shared by >= k source rows?
    value_rows, token_rows = defaultdict(set), defaultdict(set)
    for v, row in node.samples:
        value_rows[str(v)].add(row)
        for tok in re.findall(r"\w+", str(v)):
            token_rows[tok].add(row)
    node.common_values = {v for v, rows in value_rows.items() if len(rows) >= k}
    node.common_tokens = {t for t, rows in token_rows.items() if len(rows) >= k}

    node.kind = classify(node, k)
    values = [v for v, _ in node.samples]

    if node.kind == "category":
        common = sorted(node.common_values)
        node.common = common
        node.weights = [len(value_rows[v]) for v in common]
    if node.kind in ("text", "city"):
        lengths = [len(str(v)) for v in values]
        node.avg_len = sum(lengths) / len(lengths)
        node.templates = [str(v) for v in values]
        # Templates grouped by the source row's country, so a French row
        # borrows the shape of a French value ("Paris, Ile-de-France, France").
        node.templates_by_cc = defaultdict(list)
        for v, row in node.samples:
            node.templates_by_cc[row_cc[row]].append(str(v))
        node.lower_common = sorted(t for t in node.common_tokens if t.islower() and len(t) >= 5)
        node.place_like = any(w in ".".join(node.path).lower()
                              for w in ("city", "location", "address", "headquarter"))
    if node.kind in ("int", "float"):
        node.nums = [float(v) for v in values]
        node.as_string = isinstance(values[0], str)
        node.is_year = node.kind == "int" and \
            sum(1000 <= n <= 2100 for n in node.nums) / len(node.nums) >= 0.9
    if node.kind == "date":
        days = sorted(str(v)[:10] for v in values)
        node.dmin, node.dmax = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        node.has_time = any(len(str(v)) > 10 for v in values)


def describe(node, depth=0):
    pad = "  " * depth
    name = node.path[-1] if node.path else "(root)"
    if node.kind == "list":
        print(f"{pad}{name:30s} list     fill {node.fill:4.0%}")
        if node.items:
            describe(node.items, depth + 1)
    elif node.kind == "dict":
        print(f"{pad}{name:30s} object   fill {node.fill:4.0%}")
        for child in node.keys.values():
            describe(child, depth + 1)
    else:
        kind = node.kind
        if kind == "text":
            kind = "longtext" if node.avg_len > LONG_TEXT else "text"
        extra = f"  ({len(node.common)} shared values)" if node.kind == "category" else ""
        print(f"{pad}{name:30s} {kind:8s} fill {node.fill:4.0%}{extra}")


# ---------------------------------------------------------------------------
# 2. Generating
# ---------------------------------------------------------------------------
def slug(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", t.lower())


class Synth:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        Faker.seed(seed)
        self.fakers = {}

    def faker(self, locale):
        if locale not in self.fakers:
            self.fakers[locale] = Faker(locale)
        return self.fakers[locale]

    def short_id(self):
        return uuid.UUID(int=self.rng.getrandbits(128)).hex[:12]

    def mimic(self, node, fake, cc):
        """Take a real value as a template; replace every rare word."""
        pool = node.templates_by_cc.get(cc) or node.templates
        toks = TOKEN_RE.findall(self.rng.choice(pool))

        def rare_cap(t):
            return t[0].isupper() and t not in node.common_tokens

        out, i = [], 0
        while i < len(toks):
            tok = toks[i]
            if not (tok[0].isalnum() or tok[0] == "_") or tok in node.common_tokens:
                out.append(tok)                                   # punctuation or shared word
            elif tok.isdigit():
                out.append("".join(str(self.rng.randint(0, 9)) for _ in tok))
            elif tok[0].isupper():
                # A run of rare capitalised words ("Rio de Janeiro" minus the
                # shared "de", "Emma Watson") is one name or place: replace
                # the whole run with a single fake one.
                while (i + 2 < len(toks) and toks[i + 1] == " " and rare_cap(toks[i + 2])):
                    i += 2
                out.append(fake.city() if node.place_like else fake.last_name())
            elif node.lower_common:
                out.append(self.rng.choice(node.lower_common))    # on-topic shared word
            else:
                out.append(fake.word())
            i += 1
        return "".join(out)

    def scalar(self, node, fake, ctx):
        rng, kind = self.rng, node.kind
        if kind == "person":
            return ctx.setdefault("person", fake.name())
        if kind == "company":
            return fake.company()
        if kind == "school":
            return f"{fake.last_name()} University"
        if kind == "email":
            return fake.email()
        if kind == "phone":
            return fake.phone_number()
        if kind == "url":
            return f"https://example.invalid/{node.path[-1].lower()}/{self.short_id()}"
        if kind == "id":
            return self.short_id()
        if kind == "bool":
            return rng.random() < 0.5
        if kind == "date":
            span = max(0, (node.dmax - node.dmin).days)
            d = node.dmin + timedelta(days=rng.randint(0, span))
            if node.has_time:
                return datetime(d.year, d.month, d.day, rng.randint(0, 23),
                                rng.randint(0, 59)).isoformat(sep=" ")
            return d.isoformat()
        if kind in ("int", "float"):
            # Resample a real value with a little noise: same distribution
            # shape, no exact values copied.
            base = rng.choice(node.nums)
            lo, hi = min(node.nums), max(node.nums)
            if node.is_year:
                # A year: shift a little. Placeholders like 0 stay as they are.
                v = base + rng.randint(-2, 2) if base >= 1000 else base
            else:
                v = base * rng.uniform(0.8, 1.2)     # a count or amount
            v = min(max(v, lo), hi)                  # never outside the real range
            v = int(round(v)) if kind == "int" else round(v, 2)
            return str(v) if node.as_string else v
        if kind == "category":
            pool = ctx.get("conditional", {}).get(node.path)
            if pool:
                return rng.choices(pool[0], weights=pool[1])[0]
            return rng.choices(node.common, weights=node.weights)[0]
        if kind == "text":
            if node.avg_len > LONG_TEXT:
                return fake.text(max_nb_chars=max(60, int(node.avg_len)))
            return self.mimic(node, fake, ctx.get("cc"))
        return None

    def value(self, node, fake, ctx, force=False):
        if not force and self.rng.random() > node.fill:
            return None
        if node.kind == "list":
            n = self.rng.choice(node.list_lengths) if node.list_lengths else 1
            if node.items is None:
                return []
            return [self.value(node.items, fake, ctx, force=True) for _ in range(n)]
        if node.kind == "dict":
            obj = {}
            for key, child in node.keys.items():
                # A key that appeared in only some objects (a typo, an
                # optional field) appears in the same share of generated ones.
                if self.rng.random() < child.seen / max(node.dict_count, 1):
                    obj[key] = self.value(child, fake, ctx)
            derive_ids(obj)
            return obj
        if node.kind == "empty":
            return None
        return self.scalar(node, fake, ctx)


def hyphen_slug(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]


def derive_ids(obj):
    """An id field next to a name field ('company_id' beside 'company', or
    'current_company:company_id' beside 'current_company:name') becomes a
    slug of that name, as in the real data, instead of a random string."""
    for key in list(obj):
        if not (key.endswith("_id") and isinstance(obj[key], str)):
            continue
        base = key[:-3]
        candidates = [base, base + "_name", base + "name"]
        if ":" in key:
            candidates.append(key.split(":", 1)[0] + ":name")
        for c in candidates:
            if isinstance(obj.get(c), str) and obj[c]:
                obj[key] = hyphen_slug(obj[c])
                break


def find_column(columns, *names):
    low = {c.lower(): c for c in columns}
    return next((low[n] for n in names if n in low), None)


def pin_hash_seed():
    """Some Faker locales (Italian cities, for one) return a different order
    in every Python process, which breaks --seed reproducibility. Python only
    reads PYTHONHASHSEED at startup, so restart this script once with it set."""
    if os.environ.get("PYTHONHASHSEED") != "0":
        env = dict(os.environ, PYTHONHASHSEED="0")
        sys.exit(subprocess.call([sys.executable] + sys.argv, env=env))


def main():
    pin_hash_seed()
    ap = argparse.ArgumentParser(description="Generate synthetic data shaped like a CSV.")
    ap.add_argument("csv", help="source CSV to learn the structure from")
    ap.add_argument("--n", type=int, default=400, help="rows to generate")
    ap.add_argument("--seed", type=int, default=6102, help="same seed, same output")
    ap.add_argument("--min-count", type=int, default=5,
                    help="a real value or word is reused only if at least this many rows share it")
    ap.add_argument("--out", help="output file name without extension")
    ap.add_argument("--show-schema", action="store_true",
                    help="print what the script decided about each column")
    args = ap.parse_args()
    k = args.min_count

    src = Path(args.csv)
    with open(src, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames
        rows = [{c: parse_cell(r.get(c)) for c in columns} for r in reader]

    # Learn the structure.
    schema = {c: Node((c,)) for c in columns}
    for i, r in enumerate(rows):
        for c in columns:
            schema[c].observe(r[c], i)
    country_col = find_column(columns, "country_code", "country")
    row_cc = [r[country_col] if country_col else None for r in rows]
    for node in schema.values():
        finalize(node, k, row_cc)

    if args.show_schema:
        print(f"Learned from {len(rows)} rows of {src.name}:\n")
        for node in schema.values():
            describe(node)
        print()

    # If there is a country column, it picks each row's Faker locale and
    # keeps category columns such as region consistent with it. Text columns
    # such as city stay consistent through per-country templates instead.
    conditional = {}
    cc_total = Counter(r[country_col] for r in rows) if country_col else Counter()
    cc_present = Counter()
    if country_col and schema[country_col].kind == "category":
        for r in rows:
            for c in columns:
                if r[c] is not None:
                    cc_present[(c, r[country_col])] += 1
        pair_rows = defaultdict(lambda: defaultdict(set))
        for i, r in enumerate(rows):
            cc = r[country_col]
            if cc is None:
                continue
            for c in columns:
                if c != country_col and schema[c].shape == "scalar" and r[c] is not None:
                    pair_rows[(c, cc)][str(r[c])].add(i)
        for (c, cc), vals in pair_rows.items():
            keep = [(v, len(s)) for v, s in vals.items() if len(s) >= k]
            if keep and schema[c].kind == "category":
                conditional[((c,), cc)] = ([v for v, _ in keep], [n for _, n in keep])

    id_col = find_column(columns, "id")
    id_unique = bool(id_col) and len({r[id_col] for r in rows}) == len(rows)

    # Generate.
    synth = Synth(args.seed)
    out, used_ids = [], set()
    for _ in range(args.n):
        ctx, row, cc = {}, {}, None
        if country_col:
            cc = synth.value(schema[country_col], synth.faker("en_US"), ctx)
            row[country_col] = cc
            ctx["cc"] = cc
            ctx["conditional"] = {p: pool for (p, code), pool in conditional.items() if code == cc}
        fake = synth.faker(LOCALES.get(str(cc).upper(), "en_US"))
        for c in columns:
            if c == country_col:
                continue
            node = schema[c]
            if node.kind == "category" and cc is not None and cc_total[cc] >= k:
                # Fill rate and values both follow the country: e.g. rows
                # coded "UK" never have a region in the source, so neither
                # do generated "UK" rows.
                if synth.rng.random() >= cc_present[(c, cc)] / cc_total[cc]:
                    row[c] = None
                else:
                    row[c] = synth.scalar(node, fake, ctx)
            else:
                row[c] = synth.value(node, fake, ctx)

        derive_ids(row)
        # A unique id column becomes a readable key built from the person's name.
        if id_unique:
            base = slug(ctx.get("person") or fake.name()) or "row"
            pid, i = base, 2
            while pid in used_ids:
                pid, i = f"{base}{i}", i + 1
            used_ids.add(pid)
            row[id_col] = pid
        out.append({c: row[c] for c in columns})

    # Write JSON and CSV.
    stem = args.out or f"{src.stem}_synthetic"
    with open(f"{stem}.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    with open(f"{stem}.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(columns)
        for r in out:
            w.writerow(["" if r[c] is None
                        else json.dumps(r[c], ensure_ascii=False) if isinstance(r[c], (list, dict))
                        else str(r[c]) for c in columns])

    print(f"wrote {len(out)} rows -> {stem}.json, {stem}.csv")


if __name__ == "__main__":
    main()
