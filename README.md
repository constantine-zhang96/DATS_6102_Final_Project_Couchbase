# Synthetic Profile Generator

DATS 6102 Data Warehousing, Fall 2026, Group 3 (Couchbase Community Edition)

`generate_profiles.py` reads a CSV and produces a synthetic dataset with the
same columns, the same nested JSON structure and roughly the same
distributions, without reproducing any real person.

We built it because the professor approved our dataset on the condition that
we use synthetic candidate profiles instead of real LinkedIn profiles, with
300 to 500 records being enough to demonstrate Couchbase.

Nothing in the script is tied to the LinkedIn file. It works out the schema
from whatever CSV it is given; it also runs unchanged on the company dataset.

---

## Quick start

```
pip install faker
python generate_profiles.py LinkedIn_people_profiles_datasets.csv --n 400 --seed 6102 --out profiles_synthetic
```

This produces:

| File | Contents |
|---|---|
| `profiles_synthetic.json` | JSON array, nested fields as real JSON. Import into Couchbase with **Parse File As = JSON List**. |
| `profiles_synthetic.csv` | Same records in the original CSV layout, nested fields as JSON text. Useful for `cbimport csv` or the MySQL comparison. |

If you use PyCharm, run `pip install faker` in PyCharm's own Terminal tab so
it installs into the project's Python interpreter.

Output files are written to the folder you run the command from. If the CSV
is elsewhere, pass its path:

```
python generate_profiles.py C:\couchbase\LinkedIn_people_profiles_datasets.csv --out profiles_synthetic
```

## Options

| Option | Meaning | Default |
|---|---|---|
| `csv` | Source CSV to learn the structure from (required) | |
| `--n` | Number of rows to generate | 400 |
| `--seed` | Random seed. Same seed and same input give byte-identical output | 6102 |
| `--out` | Output file name, without extension | `<input>_synthetic` |
| `--show-schema` | Print what the script decided each column is | off |
| `--min-count` | How many source rows must share a word or value before it may be reused | 5 |

`python generate_profiles.py --help` prints the same list.

**Group rule:** everyone uses `--n 400 --seed 6102`. Different values give
different data, and our environments would no longer match.

---

## How it works

### 1. Learn the structure

The script reads every row and builds a description of each field, including
fields nested inside JSON (for example `experience[].positions[].title`).
For each field it records:

* how often it is filled in
* how long lists usually are
* which keys appear inside objects, and how often
* the values themselves, and which source rows they came from

### 2. Classify each field

Every field gets a kind, decided from its name and its values:

| Kind | Example fields | How it is detected |
|---|---|---|
| person / company / school | `name`, `current_company:name`, `experience[].company` | field name, plus words like Inc, LLC, GmbH |
| id | `id`, `company_id` | name is `id` or ends in `_id` |
| url | `url`, `avatar`, `posts[].link` | values start with `http` |
| date | `timestamp` | values look like `YYYY-MM-DD` |
| int / float | `following`, `recommendations_count`, `education[].end_year` | values are numbers |
| category | `country_code`, `region`, `experience[].industry` | most values are shared by at least 5 rows |
| text | `position`, `city`, `education[].degree` | anything else, average under 100 characters |
| long text | `about`, `experience[].positions[].description` | anything else, average over 100 characters |

Run with `--show-schema` to see the decision for every column.

### 3. Generate

| Kind | Taken from the CSV | Written to the output |
|---|---|---|
| Names, emails, phones, URLs, ids | Only the country, to choose a Faker locale | Entirely generated. A Brazilian row gets a fake Brazilian name. |
| Dates | Earliest and latest date | Random date inside that range |
| Numbers | The real values | A real value picked at random, nudged ±20% (years ±2), kept inside the real min and max |
| Categories | Values and their frequencies | Real values at real frequencies, only those shared by at least 5 rows |
| Short text | A real value as a template | Same shape; every word shared by fewer than 5 rows is replaced |
| Long text | Average length only | New Faker text of similar length |

Structure always follows the source: column names and order, nesting, fill
rates, list lengths, and which keys appear in nested objects. A key that
appears in only some source objects, including a typo such as `attribuion`,
appears in roughly the same share of generated objects.

### 4. Keep rows consistent

* **Country drives the row.** It picks the Faker locale, so names and
  generated place names fit the country.
* **Region follows country.** Regions are drawn from the regions seen with
  that country. Rows coded `UK` have no region in the source, so generated
  `UK` rows have none either.
* **City follows country.** City templates are taken only from source rows
  with the same country.
* **Ids follow names.** The document key `id` is built from the generated
  person's name (`Adrien-Léon Grenier` becomes `adrienleongrenier`), and
  `company_id` is a slug of the company name beside it, matching how the
  source data looks.

---

## Privacy

`k` below is `--min-count`, default 5.

1. Names, emails, phone numbers, URLs and ids are always generated. All URLs
   use the reserved `example.invalid` domain, which can never resolve.
2. Category values are reused only if at least `k` different source rows
   share them.
3. Short text keeps a real value's shape, but a word is kept only if at least
   `k` different rows contain it. Personal names and small employers are rare
   words, so they are always replaced.
4. Long text is never copied.

**A known consequence of rule 3:** a generic phrase made only of common words,
such as `Director of Finance & Corporate Controller` or
`University of Oxford`, can come out identical to a real value. By
construction such a phrase contains no rare word, so it cannot point to a
specific person.

### Verification on the LinkedIn people file

| Check | Result |
|---|---|
| Columns identical to the source, same order | yes, all 26 |
| Document ids unique | yes, 400 of 400 |
| Real full names in the `name` column | 0 |
| Real first + last name pairs anywhere in the output, nested fields included | 0 of 919 |
| Real `linkedin.com` or `licdn.com` URLs | none |
| Same seed run twice gives identical files | yes |

---

## Things to know

* **Reproducibility on every machine.** Some Faker locales (Italian city
  names, for one) return data in a different order in each Python process,
  which would break `--seed`. The script restarts itself once with
  `PYTHONHASHSEED=0` to prevent this. You do not need to do anything.
* **`NA` is not treated as missing.** In this dataset `NA` means North
  America. Many tools, pandas included, read `NA` as null and silently drop
  those values. The script only treats empty cells, `null`, `None` and `NaN`
  as missing. This is a real CSV weakness worth mentioning in the MySQL
  comparison.
* **Colons in field names.** `current_company:company_id` and
  `current_company:name` come from the source. In SQL++ they must be wrapped
  in backticks: `` SELECT `current_company:name` FROM ... ``.
* **Cyrillic letter in a column name.** The last column, `сourses`, starts
  with a Cyrillic `с` in the source file. It is kept as is so the schema
  matches.

## Limitations

The data is meant to demonstrate structure and querying, not to support
analysis. Query results such as "most common industry" describe the
generator, not the labour market.

* Fields are mostly generated independently. A job title does not depend on
  the company's industry, and dates inside `experience` are not ordered.
* US city names are invented by Faker (for example `Thomasmouth`); many
  other locales produce real town names.
* Countries without a Latin-script Faker locale (Japan, Korea, Egypt and
  others) fall back to English names so that ids stay readable.
* Short text built from shape templates can read awkwardly when the source
  value was long or mixed several languages.
* The source CSV needs a header row, and nested fields must be valid JSON.

