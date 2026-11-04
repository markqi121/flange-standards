#!/usr/bin/env python3
"""Build the standalone flange-standards data package.

Inputs (committed in this package):
  data/standards.json          standard metadata exported from the Website
  data/dimensions/*.json       one file per dimension dataset
  data/reference/*.json        one file per reference document

Outputs (generated, safe to delete and rebuild):
  csv/                         flat tables for Excel / generic import
  sqlite/flange-standards.sqlite
  data/manifest.json           counts, versions and SHA-256 checksums
  docs/coverage.md             what is in the package, per standard / topic
  docs/sources.md              edition and table/page provenance per standard

Usage:  python3 scripts/build.py
Requires: Python 3.8+ standard library only.
"""

import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PKG_ROOT / "data"
CSV_DIR = PKG_ROOT / "csv"
REF_CSV_DIR = CSV_DIR / "reference"
SQLITE_DIR = PKG_ROOT / "sqlite"
DOCS_DIR = PKG_ROOT / "docs"
DB_PATH = SQLITE_DIR / "flange-standards.sqlite"

TOPIC_ORDER = [
    "pressure-ratings",
    "material-groups",
    "tolerances",
    "marking",
    "changes",
    "types",
    "facings",
]

INCH_FIELDS = [
    "od",
    "th",
    "rf",
    "rf_height",
    "bc",
    "bolt_hole",
    "hub",
    "length",
    "hub_weld",
]

# ASME dimensions are inch-based in the published standard; EN and JIS are metric.
INCH_STANDARDS = {"asme-b16-5", "asme-b16-47-a", "asme-b16-47-b"}

DIMENSION_FIELDS = [
    "std",
    "type",
    "size",
    "size_label",
    "dn",
    "rating",
    "rating_label",
    "od",
    "th",
    "rf",
    "rf_height",
    "bc",
    "bolts",
    "bolt_hole",
    "hub",
    "length",
    "hub_weld",
    "weight",
]

# Blind flanges do not publish hub / length / hub_weld; those keys may be absent.
OPTIONAL_DIMENSION_FIELDS = {"hub", "length", "hub_weld"}


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(message):
    print(f"build: ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def fmt(value):
    """Format a number for CSV without trailing zeros."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


def mm_to_in(value):
    if value is None:
        return None
    return round(value / 25.4, 4)


def load_package():
    standards_doc = read_json(DATA_DIR / "standards.json")
    standards = {item["id"]: item for item in standards_doc["standards"]}
    reference_standards = {
        item["id"]: item for item in standards_doc["referenceStandards"]
    }

    dimensions = {}
    for path in sorted((DATA_DIR / "dimensions").glob("*.json")):
        rows = read_json(path)
        if not isinstance(rows, list):
            fail(f"{path} must contain a JSON array of dimension rows")
        dimensions[path.stem] = rows

    references = {}
    for path in sorted((DATA_DIR / "reference").glob("*.json")):
        doc = read_json(path)
        if not isinstance(doc, dict) or "id" not in doc or "topics" not in doc:
            fail(f"{path} is not a reference document")
        references[doc["id"]] = doc

    return standards_doc, standards, reference_standards, dimensions, references


def validate_inputs(standards, reference_standards, dimensions, references):
    if not standards:
        fail("data/standards.json contains no standards")

    for std, meta in standards.items():
        doc_id = meta.get("referenceDocumentId", std)
        if doc_id not in references:
            fail(f"standard {std} points to missing reference document {doc_id}")

    for std, rows in dimensions.items():
        if std not in standards:
            fail(f"dimension dataset {std} has no entry in standards.json")
        seen = set()
        for row in rows:
            missing = [
                field
                for field in DIMENSION_FIELDS
                if field not in OPTIONAL_DIMENSION_FIELDS and field not in row
            ]
            if missing:
                fail(f"{std}: row {row.get('size')} missing fields {missing}")
            key = (row["type"], row["size"], row["rating"])
            if key in seen:
                fail(f"{std}: duplicate row {key}")
            seen.add(key)

    for doc_id, doc in references.items():
        if doc_id not in reference_standards:
            fail(f"reference document {doc_id} has no entry in standards.json")
        for topic in doc["topics"]:
            if topic not in TOPIC_ORDER:
                fail(f"{doc_id}: unknown topic {topic}")
            for section in doc["topics"][topic]["sections"]:
                table = section.get("table")
                if not table:
                    continue
                width = len(table["columns"])
                for index, row in enumerate(table["rows"]):
                    if len(row) != width:
                        fail(
                            f"{doc_id}/{topic}: table '{table['caption']}' "
                            f"row {index} has {len(row)} cells, expected {width}"
                        )
                    if not all(isinstance(cell, str) for cell in row):
                        fail(
                            f"{doc_id}/{topic}: table '{table['caption']}' "
                            f"row {index} contains a non-string cell"
                        )


def standard_ratings(rows):
    """Distinct ratings in first-appearance order (the datasets are in standard order)."""
    ordered = []
    for row in rows:
        if row["rating"] not in ordered:
            ordered.append(row["rating"])
    return ordered


def standard_types(rows):
    ordered = []
    for row in rows:
        if row["type"] not in ordered:
            ordered.append(row["type"])
    return ordered


def write_dimension_csv(standards, dimensions):
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    path = CSV_DIR / "dimensions.csv"

    header = [
        "std",
        "std_name",
        "type",
        "type_name",
        "size",
        "size_label",
        "dn",
        "rating",
        "rating_label",
        "od_mm",
        "th_mm",
        "rf_mm",
        "rf_height_mm",
        "bc_mm",
        "bolts",
        "bolt_hole_mm",
        "hub_mm",
        "length_mm",
        "hub_weld_mm",
        "weight_kg",
        "od_in",
        "th_in",
        "rf_in",
        "rf_height_in",
        "bc_in",
        "bolt_hole_in",
        "hub_in",
        "length_in",
        "hub_weld_in",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for std in standards:
            meta = standards[std]
            for row in dimensions.get(std, []):
                values = [
                    std,
                    meta["name"],
                    row["type"],
                    meta["typeNames"].get(row["type"], row["type"]),
                    row["size"],
                    row["size_label"],
                    row["dn"],
                    row["rating"],
                    row["rating_label"],
                ]
                values += [fmt(row.get(field)) for field in INCH_FIELDS[:4]]
                values += [fmt(row.get("bc")), fmt(row.get("bolts")), fmt(row.get("bolt_hole"))]
                values += [fmt(row.get("hub")), fmt(row.get("length")), fmt(row.get("hub_weld"))]
                values += [fmt(row.get("weight"))]
                if std in INCH_STANDARDS:
                    values += [
                        fmt(mm_to_in(row.get(field)))
                        for field in INCH_FIELDS
                    ]
                else:
                    values += [""] * len(INCH_FIELDS)
                writer.writerow(values)

    return path, sum(len(rows) for rows in dimensions.values())


def write_dimension_summary_csv(standards, dimensions):
    path = CSV_DIR / "dimension-summary.csv"
    header = [
        "std",
        "std_name",
        "edition",
        "rows",
        "types",
        "ratings",
        "sizes",
        "dn_min",
        "dn_max",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for std, meta in standards.items():
            rows = dimensions.get(std, [])
            if not rows:
                continue
            dns = sorted(row["dn"] for row in rows)
            writer.writerow(
                [
                    std,
                    meta["name"],
                    meta.get("dimensionEdition") or meta["edition"],
                    len(rows),
                    " | ".join(standard_types(rows)),
                    " | ".join(standard_ratings(rows)),
                    len({row["size"] for row in rows}),
                    dns[0],
                    dns[-1],
                ]
            )
    return path


def write_reference_csvs(standards_doc, reference_standards, references):
    REF_CSV_DIR.mkdir(parents=True, exist_ok=True)
    index_rows = []
    written = []

    for doc_id, doc in references.items():
        info = reference_standards[doc_id]
        for topic in TOPIC_ORDER:
            if topic not in doc["topics"]:
                continue
            data = doc["topics"][topic]
            file_name = f"{info['slug']}--{topic}.csv"
            path = REF_CSV_DIR / file_name

            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                def raw(line=""):
                    handle.write(f"{line}\n")

                writer = csv.writer(handle, lineterminator="\n")
                raw(f"# {data['title']}")
                raw(f"# Standard: {doc['edition']}")
                raw(f"# Verified: {doc['verifiedOn']}")
                raw(f"# Source: {'; '.join(data.get('sources', []))}")
                raw("# Reference data only. The purchased standard edition governs.")

                table_ord = 0
                for section_ord, section in enumerate(data["sections"]):
                    table = section.get("table")
                    if not table:
                        continue
                    table_ord += 1
                    raw()
                    raw(f"# {table['caption']}")
                    if table.get("note"):
                        raw(f"# Note: {table['note']}")
                    writer.writerow([column["label"] for column in table["columns"]])
                    writer.writerows(table["rows"])
                    index_rows.append(
                        {
                            "std": doc_id,
                            "std_name": info["name"],
                            "edition": doc["edition"],
                            "verified_on": doc["verifiedOn"],
                            "topic": topic,
                            "topic_label": standards_doc["topicLabels"].get(topic, topic),
                            "title": data["title"],
                            "section_ord": section_ord,
                            "table_ord": table_ord,
                            "caption": table["caption"],
                            "column_count": len(table["columns"]),
                            "row_count": len(table["rows"]),
                            "sources": "; ".join(data.get("sources", [])),
                        }
                    )

            written.append(path)

    index_path = CSV_DIR / "reference-index.csv"
    fieldnames = [
        "std",
        "std_name",
        "edition",
        "verified_on",
        "topic",
        "topic_label",
        "title",
        "section_ord",
        "table_ord",
        "caption",
        "column_count",
        "row_count",
        "sources",
    ]
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(index_rows)

    return written, index_path, index_rows


SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE standard (
    id                 TEXT PRIMARY KEY,
    slug               TEXT NOT NULL,
    name               TEXT NOT NULL,
    short              TEXT NOT NULL,
    edition            TEXT NOT NULL,
    dimension_edition  TEXT,
    reference_document_id TEXT,
    edition_note       TEXT,
    region             TEXT,
    rating_name        TEXT,
    size_syntax        TEXT,
    base_material      TEXT,
    face_note          TEXT,
    rating_note        TEXT,
    intro_json         TEXT,
    material_options_json TEXT
);

CREATE TABLE standard_type (
    std          TEXT NOT NULL REFERENCES standard(id),
    type         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    note         TEXT,
    PRIMARY KEY (std, type)
);

CREATE TABLE standard_rating (
    std   TEXT NOT NULL REFERENCES standard(id),
    rating TEXT NOT NULL,
    label TEXT NOT NULL,
    ord    INTEGER NOT NULL,
    PRIMARY KEY (std, rating)
);

CREATE TABLE standard_size (
    std        TEXT NOT NULL REFERENCES standard(id),
    size       TEXT NOT NULL,
    size_label TEXT NOT NULL,
    dn         INTEGER NOT NULL,
    PRIMARY KEY (std, size)
);

CREATE TABLE flange_dimension (
    id            INTEGER PRIMARY KEY,
    std           TEXT NOT NULL REFERENCES standard(id),
    type          TEXT NOT NULL,
    size          TEXT NOT NULL,
    size_label    TEXT NOT NULL,
    dn            INTEGER NOT NULL,
    rating        TEXT NOT NULL,
    rating_label  TEXT NOT NULL,
    od_mm         REAL,
    th_mm         REAL,
    rf_mm         REAL,
    rf_height_mm  REAL,
    bc_mm         REAL,
    bolts         INTEGER,
    bolt_hole_mm  REAL,
    hub_mm        REAL,
    length_mm     REAL,
    hub_weld_mm   REAL,
    weight_kg     REAL,
    od_in         REAL,
    th_in         REAL,
    rf_in         REAL,
    rf_height_in  REAL,
    bc_in         REAL,
    bolt_hole_in  REAL,
    hub_in        REAL,
    length_in     REAL,
    hub_weld_in   REAL,
    UNIQUE (std, type, size, rating)
);

CREATE INDEX idx_dimension_lookup ON flange_dimension (std, type, rating, dn);
CREATE INDEX idx_dimension_size   ON flange_dimension (std, size);

CREATE TABLE reference_topic (
    id               TEXT PRIMARY KEY,
    std              TEXT NOT NULL,
    topic            TEXT NOT NULL,
    title            TEXT NOT NULL,
    meta_description TEXT,
    edition          TEXT NOT NULL,
    verified_on      TEXT NOT NULL,
    source_document  TEXT,
    intro_json       TEXT,
    key_facts_json   TEXT,
    related_json     TEXT,
    UNIQUE (std, topic)
);

CREATE TABLE reference_section (
    id               INTEGER PRIMARY KEY,
    topic_id         TEXT NOT NULL REFERENCES reference_topic(id),
    ord              INTEGER NOT NULL,
    heading          TEXT NOT NULL,
    paragraphs_json  TEXT,
    bullets_json     TEXT
);

CREATE TABLE reference_table (
    id         TEXT PRIMARY KEY,
    topic_id   TEXT NOT NULL REFERENCES reference_topic(id),
    section_id INTEGER REFERENCES reference_section(id),
    ord        INTEGER NOT NULL,
    caption    TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE reference_column (
    table_id TEXT NOT NULL REFERENCES reference_table(id),
    ord      INTEGER NOT NULL,
    key      TEXT NOT NULL,
    label    TEXT NOT NULL,
    unit     TEXT,
    align    TEXT,
    PRIMARY KEY (table_id, ord)
);

CREATE TABLE reference_cell (
    table_id TEXT NOT NULL REFERENCES reference_table(id),
    row_ord  INTEGER NOT NULL,
    col_ord  INTEGER NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (table_id, row_ord, col_ord)
);

CREATE TABLE reference_footnote (
    table_id TEXT NOT NULL REFERENCES reference_table(id),
    ord      INTEGER NOT NULL,
    marker   TEXT,
    text     TEXT NOT NULL,
    PRIMARY KEY (table_id, ord)
);

CREATE TABLE reference_faq (
    topic_id TEXT NOT NULL REFERENCES reference_topic(id),
    ord      INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    PRIMARY KEY (topic_id, ord)
);

CREATE TABLE reference_source (
    topic_id TEXT NOT NULL REFERENCES reference_topic(id),
    ord      INTEGER NOT NULL,
    citation TEXT NOT NULL,
    PRIMARY KEY (topic_id, ord)
);

CREATE VIEW v_reference_cell AS
SELECT
    t.std,
    t.topic,
    tb.id      AS table_id,
    tb.caption AS table_caption,
    c.ord      AS col_ord,
    c.label    AS column_label,
    c.unit     AS column_unit,
    rc.row_ord,
    rc.value
FROM reference_cell rc
JOIN reference_table  tb ON tb.id = rc.table_id
JOIN reference_column c  ON c.table_id = rc.table_id AND c.ord = rc.col_ord
JOIN reference_topic  t   ON t.id = tb.topic_id;
"""


def json_compact(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_sqlite(standards_doc, standards, reference_standards, dimensions, references):
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(SQLITE_SCHEMA)
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("generated_from", standards_doc["generatedFrom"]),
        )

        for std, meta in standards.items():
            conn.execute(
                """
                INSERT INTO standard (
                    id, slug, name, short, edition, dimension_edition,
                    reference_document_id, edition_note,
                    region, rating_name, size_syntax, base_material, face_note,
                    rating_note, intro_json, material_options_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    std,
                    meta["slug"],
                    meta["name"],
                    meta["short"],
                    meta["edition"],
                    meta.get("dimensionEdition"),
                    meta.get("referenceDocumentId", std),
                    meta.get("editionNote"),
                    meta.get("region"),
                    meta.get("ratingName"),
                    meta.get("sizeSyntax"),
                    meta.get("baseMaterial"),
                    meta.get("faceNote"),
                    meta.get("ratingNote"),
                    json_compact(meta.get("intro")),
                    json_compact(meta.get("materialOptions")),
                ),
            )

            for type_name, display_name in meta["typeNames"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO standard_type (std, type, display_name, note) VALUES (?, ?, ?, ?)",
                    (std, type_name, display_name, meta.get("typeNotes", {}).get(type_name)),
                )

            rows = dimensions.get(std, [])
            ratings = standard_ratings(rows)
            rating_labels = {}
            for row in rows:
                rating_labels.setdefault(row["rating"], row["rating_label"])
            for order, rating in enumerate(ratings):
                conn.execute(
                    "INSERT INTO standard_rating (std, rating, label, ord) VALUES (?, ?, ?, ?)",
                    (std, rating, rating_labels[rating], order),
                )

            sizes = {}
            for row in rows:
                sizes.setdefault(row["size"], (row["size_label"], row["dn"]))
            for size, (label, dn) in sizes.items():
                conn.execute(
                    "INSERT OR REPLACE INTO standard_size (std, size, size_label, dn) VALUES (?, ?, ?, ?)",
                    (std, size, label, dn),
                )

            for row in rows:
                values = [row.get(field) for field in DIMENSION_FIELDS]
                inches = (
                    [mm_to_in(row.get(field)) for field in INCH_FIELDS]
                    if std in INCH_STANDARDS
                    else [None] * len(INCH_FIELDS)
                )
                conn.execute(
                    """
                    INSERT INTO flange_dimension (
                        std, type, size, size_label, dn, rating, rating_label,
                        od_mm, th_mm, rf_mm, rf_height_mm, bc_mm, bolts, bolt_hole_mm,
                        hub_mm, length_mm, hub_weld_mm, weight_kg,
                        od_in, th_in, rf_in, rf_height_in, bc_in, bolt_hole_in,
                        hub_in, length_in, hub_weld_in
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(values) + tuple(inches),
                )

        section_id = 0
        for doc_id, doc in references.items():
            info = reference_standards[doc_id]
            for topic in TOPIC_ORDER:
                if topic not in doc["topics"]:
                    continue
                data = doc["topics"][topic]
                topic_id = f"{doc_id}::{topic}"
                conn.execute(
                    """
                    INSERT INTO reference_topic (
                        id, std, topic, title, meta_description, edition, verified_on,
                        source_document, intro_json, key_facts_json, related_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic_id,
                        doc_id,
                        topic,
                        data["title"],
                        data.get("metaDescription"),
                        doc["edition"],
                        doc["verifiedOn"],
                        doc.get("sourceDocument"),
                        json_compact(data.get("intro")),
                        json_compact(data.get("keyFacts")),
                        json_compact(data.get("related")),
                    ),
                )

                for order, source in enumerate(data.get("sources", [])):
                    conn.execute(
                        "INSERT INTO reference_source (topic_id, ord, citation) VALUES (?, ?, ?)",
                        (topic_id, order, source),
                    )

                for order, faq in enumerate(data.get("faq", [])):
                    conn.execute(
                        "INSERT INTO reference_faq (topic_id, ord, question, answer) VALUES (?, ?, ?, ?)",
                        (topic_id, order, faq["q"], faq["a"]),
                    )

                table_ord = 0
                for section_ord, section in enumerate(data["sections"]):
                    section_id += 1
                    conn.execute(
                        "INSERT INTO reference_section (id, topic_id, ord, heading, paragraphs_json, bullets_json) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            section_id,
                            topic_id,
                            section_ord,
                            section.get("heading", ""),
                            json_compact(section.get("paragraphs")),
                            json_compact(section.get("bullets")),
                        ),
                    )
                    table = section.get("table")
                    if not table:
                        continue
                    table_ord += 1
                    table_id = f"{topic_id}::t{table_ord}"
                    conn.execute(
                        "INSERT INTO reference_table (id, topic_id, section_id, ord, caption, note) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            table_id,
                            topic_id,
                            section_id,
                            table_ord,
                            table["caption"],
                            table.get("note"),
                        ),
                    )
                    for col_ord, column in enumerate(table["columns"]):
                        conn.execute(
                            "INSERT INTO reference_column (table_id, ord, key, label, unit, align) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                table_id,
                                col_ord,
                                column["key"],
                                column["label"],
                                column.get("unit"),
                                column.get("align"),
                            ),
                        )
                    for row_ord, row in enumerate(table["rows"]):
                        for col_ord, cell in enumerate(row):
                            conn.execute(
                                "INSERT INTO reference_cell (table_id, row_ord, col_ord, value) VALUES (?, ?, ?, ?)",
                                (table_id, row_ord, col_ord, cell),
                            )
                    for fn_ord, footnote in enumerate(table.get("footnotes", [])):
                        conn.execute(
                            "INSERT INTO reference_footnote (table_id, ord, marker, text) VALUES (?, ?, ?, ?)",
                            (table_id, fn_ord, footnote.get("marker"), footnote["text"]),
                        )

        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    return DB_PATH


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_generated_files():
    for folder in (DATA_DIR, CSV_DIR, SQLITE_DIR):
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "manifest.json":
                continue
            if path.suffix == ".pyc":
                continue
            yield path


def build_manifest(standards_doc, standards, reference_standards, dimensions, references):
    by_standard = {}
    types = set()
    ratings = set()
    for std, rows in dimensions.items():
        by_standard[std] = {
            "rows": len(rows),
            "types": standard_types(rows),
            "ratings": standard_ratings(rows),
            "sizes": len({row["size"] for row in rows}),
        }
        for row in rows:
            types.add(row["type"])
            ratings.add(row["rating"])

    topics = 0
    tables = 0
    table_rows = 0
    cells = 0
    footnotes = 0
    faq = 0
    topics_by_standard = {}
    for doc_id, doc in references.items():
        topics_by_standard[doc_id] = []
        for topic, data in doc["topics"].items():
            topics += 1
            topics_by_standard[doc_id].append(topic)
            faq += len(data.get("faq", []))
            for section in data["sections"]:
                table = section.get("table")
                if not table:
                    continue
                tables += 1
                table_rows += len(table["rows"])
                cells += len(table["rows"]) * len(table["columns"])
                footnotes += len(table.get("footnotes", []))

    files = []
    for path in iter_generated_files():
        files.append(
            {
                "path": path.relative_to(PKG_ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    verified_dates = sorted({doc["verifiedOn"] for doc in references.values()})
    data_version = verified_dates[-1] if verified_dates else "unknown"

    manifest = {
        "name": "flange-standards-data",
        "version": data_version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "generatedFrom": standards_doc["generatedFrom"],
        "editions": {
            doc_id: {
                "edition": doc["edition"],
                "verifiedOn": doc["verifiedOn"],
                "sourceDocument": doc.get("sourceDocument"),
            }
            for doc_id, doc in references.items()
        },
        "counts": {
            "standards": len(standards),
            "referenceDocuments": len(references),
            "dimensionRows": sum(len(rows) for rows in dimensions.values()),
            "dimensionRowsByStandard": by_standard,
            "distinctTypes": sorted(types),
            "distinctRatings": sorted(ratings),
            "referenceTopics": topics,
            "referenceTopicsByStandard": topics_by_standard,
            "referenceTables": tables,
            "referenceTableRows": table_rows,
            "referenceCells": cells,
            "referenceFootnotes": footnotes,
            "referenceFaq": faq,
        },
        "files": files,
    }

    path = DATA_DIR / "manifest.json"
    path.write_text(f"{json.dumps(manifest, ensure_ascii=False, indent=2)}\n", "utf-8")
    return manifest, path


def write_coverage_doc(standards_doc, standards, dimensions, references):
    lines = [
        "# 数据覆盖范围",
        "",
        "> 本文件由 `scripts/build.py` 自动生成，请勿手工编辑。",
        "",
        f"- 标准数：{len(standards)}",
        f"- 尺寸数据行：{sum(len(rows) for rows in dimensions.values())}",
        f"- 参考文档：{len(references)}",
        "",
        "## 尺寸数据",
        "",
        "| 标准 | 版本 | 行数 | 类型 | 压力等级 | 尺寸数 | DN 范围 |",
        "|---|---|---|---|---|---|---|",
    ]
    for std, meta in standards.items():
        rows = dimensions.get(std, [])
        if not rows:
            continue
        dns = sorted(row["dn"] for row in rows)
        by_dn = sorted(rows, key=lambda item: item["dn"])
        size_range = f"{by_dn[0]['size_label']}–{by_dn[-1]['size_label']}"
        if size_range.startswith("DN"):
            range_text = size_range
        else:
            range_text = "{size_range} (DN{dn_min}–DN{dn_max})".format(
                size_range=size_range, dn_min=dns[0], dn_max=dns[-1]
            )
        lines.append(
            "| {name} (`{std}`) | {edition} | {count} | {types} | {ratings} | {sizes} | {size_range} |".format(
                name=meta["name"],
                std=std,
                edition=meta.get("dimensionEdition") or meta["edition"],
                count=len(rows),
                types=", ".join(standard_types(rows)),
                ratings=", ".join(standard_ratings(rows)),
                sizes=len({row["size"] for row in rows}),
                size_range=range_text,
            )
        )

    lines += [
        "",
        "## 参考表",
        "",
        "| 标准 | Topic | 表格数 | 数据行 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for doc_id, doc in references.items():
        for topic in TOPIC_ORDER:
            if topic not in doc["topics"]:
                continue
            data = doc["topics"][topic]
            tables = [s["table"] for s in data["sections"] if s.get("table")]
            table_rows = sum(len(table["rows"]) for table in tables)
            lines.append(
                f"| {doc_id} | {topic} | {len(tables)} | {table_rows} | {data['title']} |"
            )

    lines += [
        "",
        "## 覆盖限制（标准本身的规定，不是缺数据）",
        "",
        "- ASME B16.5：Class 2500 没有滑套法兰（slip-on）；Class 1500 的滑套法兰只到 NPS 2-1/2；Class 900/1500/2500 没有 NPS 3-1/2。",
        "- ASME B16.47：只定义对焊法兰（weld-neck）和盲板法兰（blind），没有滑套法兰；Class 900 只到 NPS 48；Series B NPS 38 以上按 General Note (h) 采用 Series A 尺寸。",
        "- JIS B2220：30K 只到 400A；16K/20K 不含 175A、225A。",
        "- EN 1092-1：尺寸表覆盖 DN10–DN600、PN6–PN40；PN6 从 DN10 起。",
        "",
        "> 尺寸和额定值以购买的标准版本为准；本数据包用于查询和预选型，不能替代标准原文。",
        "",
    ]

    path = DOCS_DIR / "coverage.md"
    path.write_text("\n".join(lines), "utf-8")
    return path


def write_sources_doc(standards_doc, standards, reference_standards, references):
    lines = [
        "# 数据来源与核对记录",
        "",
        "> 本文件由 `scripts/build.py` 自动生成，请勿手工编辑。",
        "> 每个标准版本、来源文档、参考表的表号与页码均来自转录时的记录。",
        "",
    ]
    for doc_id, doc in references.items():
        info = reference_standards[doc_id]
        lines += [
            f"## {info['name']}",
            "",
            f"- 版本：{doc['edition']}",
            f"- 核对日期：{doc['verifiedOn']}",
            f"- 来源文档：{doc.get('sourceDocument', '—')}",
            "",
            "### 参考表来源",
            "",
        ]
        for topic in TOPIC_ORDER:
            if topic not in doc["topics"]:
                continue
            data = doc["topics"][topic]
            lines.append(f"**{standards_doc['topicLabels'].get(topic, topic)}**（`{topic}`）")
            lines.append("")
            for source in data.get("sources", []):
                lines.append(f"- {source}")
            lines.append("")

    dimension_source_docs = {
        "asme-b16-5": "asme-b16-5-sources.md",
        "asme-b16-47-a": "asme-b16-47-sources.md",
        "asme-b16-47-b": "asme-b16-47-sources.md",
        "jis-b2220": "jis-b2220-sources.md",
        "en-1092-1": "en-1092-1-sources.md",
    }
    lines += [
        "## 尺寸数据",
        "",
        "| 标准 | 版本 | 来源说明 |",
        "|---|---|---|",
    ]
    for std, meta in standards.items():
        source_doc = dimension_source_docs.get(std, f"{std}-sources.md")
        lines.append(
            f"| {meta['name']} | {meta.get('dimensionEdition') or meta['edition']} | "
            f"逐单元格对照标准 SI 与英制表转录；详见站点 `docs/standards/{source_doc}` |"
        )

    lines += [
        "",
        "## 版本策略",
        "",
        "- ASME B16.5 / B16.47：2025 版（尺寸表与参考表均按 2025 版核对）。",
        "- EN 1092-1：2018 版。",
        "- JIS B2220：2012 版。",
        "",
        "> 每个参考表的 `sources` 字段保留了原标准表号与 PDF 页码，可用于复核。",
        "",
    ]

    path = DOCS_DIR / "sources.md"
    path.write_text("\n".join(lines), "utf-8")
    return path


def main():
    standards_doc, standards, reference_standards, dimensions, references = load_package()
    validate_inputs(standards, reference_standards, dimensions, references)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    dim_csv, dim_rows = write_dimension_csv(standards, dimensions)
    summary_csv = write_dimension_summary_csv(standards, dimensions)
    ref_csvs, ref_index, ref_index_rows = write_reference_csvs(
        standards_doc, reference_standards, references
    )
    db_path = build_sqlite(standards_doc, standards, reference_standards, dimensions, references)
    manifest, manifest_path = build_manifest(
        standards_doc, standards, reference_standards, dimensions, references
    )
    coverage = write_coverage_doc(standards_doc, standards, dimensions, references)
    sources = write_sources_doc(standards_doc, standards, reference_standards, references)

    print("Flange standards data package built:")
    print(f"  dimension rows : {dim_rows} -> {dim_csv.relative_to(PKG_ROOT)}")
    print(f"  reference CSVs : {len(ref_csvs)} -> {REF_CSV_DIR.relative_to(PKG_ROOT)}/")
    print(f"  reference index: {ref_index.relative_to(PKG_ROOT)}")
    print(f"  sqlite         : {db_path.relative_to(PKG_ROOT)}")
    print(f"  manifest       : {manifest_path.relative_to(PKG_ROOT)}")
    print(f"  docs           : {coverage.relative_to(PKG_ROOT)}, {sources.relative_to(PKG_ROOT)}")
    print(
        "  totals         : "
        f"{manifest['counts']['dimensionRows']} dimensions, "
        f"{manifest['counts']['referenceTopics']} reference topics, "
        f"{manifest['counts']['referenceTables']} tables, "
        f"{manifest['counts']['referenceCells']} cells"
    )


if __name__ == "__main__":
    main()
