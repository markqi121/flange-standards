#!/usr/bin/env python3
"""Validate the standalone flange-standards data package.

Checks, without touching the Website:
  * required files and JSON parse cleanly
  * dimension schema, duplicates, numeric ranges and cross-type bolt patterns
  * reference topics: known keys, table widths, string cells, captions, sources
  * CSV row counts against the source JSON
  * SQLite: integrity, foreign keys, row counts against JSON
  * manifest: SHA-256 checksums and counts

Usage:  python3 scripts/validate.py
Exit code 0 = all checks passed.
"""

import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PKG_ROOT / "data"
CSV_DIR = PKG_ROOT / "csv"
DB_PATH = PKG_ROOT / "sqlite" / "flange-standards.sqlite"
MANIFEST_PATH = DATA_DIR / "manifest.json"

TOPIC_ORDER = [
    "pressure-ratings",
    "material-groups",
    "tolerances",
    "marking",
    "changes",
    "types",
    "facings",
]

REQUIRED_DIMENSION_FIELDS = [
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
]

# Blind flanges legitimately omit these keys.
OPTIONAL_DIMENSION_FIELDS = {"hub", "length", "hub_weld"}

KNOWN_TYPES = {"weld-neck", "slip-on", "blind"}

errors = []
warnings = []


def check(condition, message):
    if not condition:
        errors.append(message)


def warn(condition, message):
    if not condition:
        warnings.append(message)


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_layout():
    required = [
        DATA_DIR / "standards.json",
        DATA_DIR / "manifest.json",
        CSV_DIR / "dimensions.csv",
        CSV_DIR / "dimension-summary.csv",
        CSV_DIR / "reference-index.csv",
        DB_PATH,
        PKG_ROOT / "README.md",
        PKG_ROOT / "docs" / "fields.md",
        PKG_ROOT / "docs" / "query-guide.md",
        PKG_ROOT / "docs" / "legal.md",
        PKG_ROOT / "schema" / "dimensions.schema.json",
        PKG_ROOT / "schema" / "reference.schema.json",
    ]
    for path in required:
        check(path.exists(), f"missing required file: {path.relative_to(PKG_ROOT)}")


def validate_dimensions(manifest):
    standards_doc = read_json(DATA_DIR / "standards.json")
    standards = {item["id"]: item for item in standards_doc["standards"]}
    check(len(standards) == 5, f"expected 5 standards, found {len(standards)}")

    total = 0
    by_standard = {}
    for path in sorted((DATA_DIR / "dimensions").glob("*.json")):
        std = path.stem
        rows = read_json(path)
        check(isinstance(rows, list), f"{path.name}: top level must be an array")
        check(std in standards, f"{path.name}: unknown standard id")
        by_standard[std] = len(rows)
        total += len(rows)

        seen = set()
        bolt_patterns = {}
        for row in rows:
            missing = [
                field
                for field in REQUIRED_DIMENSION_FIELDS
                if field not in row
            ]
            check(not missing, f"{std}: row {row.get('size')} missing {missing}")
            check(row["std"] == std, f"{std}: row has mismatched std {row['std']}")
            check(row["type"] in KNOWN_TYPES, f"{std}: unknown type {row['type']}")
            key = (row["type"], row["size"], row["rating"])
            check(key not in seen, f"{std}: duplicate row {key}")
            seen.add(key)

            for field in ("od", "th", "bc", "bolt_hole"):
                value = row.get(field)
                if field == "rf" and value is None:
                    continue
                check(
                    isinstance(value, (int, float)) and value > 0,
                    f"{std}/{row['size']}/{row['rating']}/{row['type']}: bad {field}={value}",
                )
            check(row["dn"] > 0, f"{std}: bad dn {row['dn']}")
            check(row["bolts"] > 0, f"{std}: bad bolts {row['bolts']}")
            check(len(row["size_label"]) > 0, f"{std}: empty size_label")
            check(len(row["rating_label"]) > 0, f"{std}: empty rating_label")

            pattern_key = (row["size"], row["rating"])
            pattern = (
                row["od"],
                row["bc"],
                row["bolts"],
                row["bolt_hole"],
            )
            if pattern_key in bolt_patterns:
                previous = bolt_patterns[pattern_key]
                same = all(
                    abs(float(a) - float(b)) < 0.051
                    for a, b in zip(pattern, previous)
                )
                check(
                    same,
                    f"{std}/{row['size']}/{row['rating']}: bolt pattern differs "
                    f"across types ({previous} vs {pattern})",
                )
            else:
                bolt_patterns[pattern_key] = pattern

    expected = manifest["counts"]["dimensionRows"]
    check(total == expected, f"dimension rows: {total} on disk, manifest says {expected}")
    check(
        by_standard == {
            key: value["rows"]
            for key, value in manifest["counts"]["dimensionRowsByStandard"].items()
        },
        "dimension rows per standard differ from manifest",
    )
    return total


def validate_references(manifest):
    docs = {}
    for path in sorted((DATA_DIR / "reference").glob("*.json")):
        doc = read_json(path)
        check(doc.get("id") == path.stem, f"{path.name}: id does not match file name")
        for field in ("id", "edition", "verifiedOn", "sourceDocument", "topics"):
            check(field in doc, f"{path.name}: missing {field}")
        docs[doc["id"]] = doc

    check(len(docs) == 4, f"expected 4 reference documents, found {len(docs)}")

    topics = 0
    tables = 0
    table_rows = 0
    cells = 0
    footnotes = 0
    faq = 0
    for doc_id, doc in docs.items():
        for topic, data in doc["topics"].items():
            topics += 1
            check(topic in TOPIC_ORDER, f"{doc_id}: unknown topic {topic}")
            for field in ("title", "intro", "sections", "sources"):
                check(field in data, f"{doc_id}/{topic}: missing {field}")
            check(len(data.get("sources", [])) > 0, f"{doc_id}/{topic}: empty sources")
            faq += len(data.get("faq", []))
            for section in data["sections"]:
                table = section.get("table")
                if not table:
                    continue
                tables += 1
                width = len(table["columns"])
                table_rows += len(table["rows"])
                cells += len(table["rows"]) * width
                footnotes += len(table.get("footnotes", []))
                check(bool(table.get("caption")), f"{doc_id}/{topic}: table without caption")
                for column in table["columns"]:
                    check(
                        bool(column.get("key")) and bool(column.get("label")),
                        f"{doc_id}/{topic}: column missing key/label",
                    )
                for index, row in enumerate(table["rows"]):
                    check(
                        len(row) == width,
                        f"{doc_id}/{topic}: '{table['caption']}' row {index} "
                        f"has {len(row)} cells, expected {width}",
                    )
                    check(
                        all(isinstance(cell, str) for cell in row),
                        f"{doc_id}/{topic}: '{table['caption']}' row {index} "
                        "contains a non-string cell",
                    )

    expected = manifest["counts"]
    check(
        topics == expected["referenceTopics"],
        f"reference topics: {topics} on disk, manifest says {expected['referenceTopics']}",
    )
    check(
        tables == expected["referenceTables"],
        f"reference tables: {tables} on disk, manifest says {expected['referenceTables']}",
    )
    check(
        cells == expected["referenceCells"],
        f"reference cells: {cells} on disk, manifest says {expected['referenceCells']}",
    )
    return {"topics": topics, "tables": tables, "rows": table_rows, "cells": cells}


def validate_csv(dimension_total, reference):
    with (CSV_DIR / "dimensions.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    check(len(rows) == dimension_total, f"dimensions.csv has {len(rows)} rows, expected {dimension_total}")
    for column in ("std", "type", "size", "rating", "od_mm", "bc_mm", "bolts"):
        check(column in header, f"dimensions.csv: missing column {column}")

    with (CSV_DIR / "reference-index.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        index_rows = list(csv.DictReader(handle))
    check(
        len(index_rows) == reference["tables"],
        f"reference-index.csv has {len(index_rows)} rows, expected {reference['tables']}",
    )

    reference_files = sorted((CSV_DIR / "reference").glob("*.csv"))
    check(
        len(reference_files) == reference["topics"],
        f"reference CSV files: {len(reference_files)}, expected {reference['topics']}",
    )


def validate_sqlite(dimension_total, reference):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        check(integrity == "ok", f"sqlite integrity_check: {integrity}")

        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        check(not foreign_keys, f"sqlite foreign key violations: {foreign_keys[:5]}")

        counts = {
            "flange_dimension": conn.execute(
                "SELECT COUNT(*) FROM flange_dimension"
            ).fetchone()[0],
            "reference_topic": conn.execute(
                "SELECT COUNT(*) FROM reference_topic"
            ).fetchone()[0],
            "reference_table": conn.execute(
                "SELECT COUNT(*) FROM reference_table"
            ).fetchone()[0],
            "reference_cell": conn.execute(
                "SELECT COUNT(*) FROM reference_cell"
            ).fetchone()[0],
        }
        check(
            counts["flange_dimension"] == dimension_total,
            f"sqlite flange_dimension: {counts['flange_dimension']}, expected {dimension_total}",
        )
        check(
            counts["reference_topic"] == reference["topics"],
            f"sqlite reference_topic: {counts['reference_topic']}, expected {reference['topics']}",
        )
        check(
            counts["reference_table"] == reference["tables"],
            f"sqlite reference_table: {counts['reference_table']}, expected {reference['tables']}",
        )
        check(
            counts["reference_cell"] == reference["cells"],
            f"sqlite reference_cell: {counts['reference_cell']}, expected {reference['cells']}",
        )

        # Spot checks: a known class 150 / NPS 6 weld-neck and a known DN100 PN16.
        row = conn.execute(
            """
            SELECT od_mm, th_mm, bc_mm, bolts, bolt_hole_mm
            FROM flange_dimension
            WHERE std='asme-b16-5' AND type='weld-neck' AND size='6' AND rating='150'
            """
        ).fetchone()
        check(row is not None, "sqlite: missing ASME B16.5 NPS 6 Class 150 weld-neck")
        if row:
            check(
                abs(row[0] - 279.4) < 0.001
                and abs(row[2] - 241.3) < 0.001
                and row[3] == 8,
                f"sqlite: unexpected ASME B16.5 NPS 6 Class 150 values {row}",
            )

        row = conn.execute(
            """
            SELECT od_mm, bc_mm, bolts
            FROM flange_dimension
            WHERE std='en-1092-1' AND type='weld-neck' AND size='dn100' AND rating='pn16'
            """
        ).fetchone()
        check(row is not None, "sqlite: missing EN 1092-1 DN100 PN16 weld-neck")
        if row:
            check(
                abs(row[0] - 220) < 0.001 and abs(row[1] - 180) < 0.001 and row[2] == 8,
                f"sqlite: unexpected EN 1092-1 DN100 PN16 values {row}",
            )

        # Every reference cell must resolve through the view to a column label.
        orphan = conn.execute(
            """
            SELECT COUNT(*) FROM reference_cell rc
            LEFT JOIN reference_column c
              ON c.table_id = rc.table_id AND c.ord = rc.col_ord
            WHERE c.ord IS NULL
            """
        ).fetchone()[0]
        check(orphan == 0, f"sqlite: {orphan} reference cells have no column definition")
    finally:
        conn.close()


def validate_manifest():
    manifest = read_json(MANIFEST_PATH)
    for field in ("name", "version", "generatedAt", "counts", "files"):
        check(field in manifest, f"manifest: missing {field}")
    check(
        manifest.get("name") == "flange-standards-data",
        "manifest: unexpected package name",
    )

    listed = {item["path"]: item for item in manifest["files"]}
    actual = set()
    for folder in (DATA_DIR, CSV_DIR, PKG_ROOT / "sqlite"):
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            actual.add(path.relative_to(PKG_ROOT).as_posix())

    missing = actual - set(listed)
    stale = set(listed) - actual
    check(not missing, f"manifest: files not listed: {sorted(missing)[:5]}")
    check(not stale, f"manifest: listed files missing: {sorted(stale)[:5]}")

    for relative, item in listed.items():
        path = PKG_ROOT / relative
        if not path.exists():
            continue
        digest = sha256_file(path)
        check(
            digest == item["sha256"],
            f"manifest: checksum mismatch for {relative}",
        )
        check(
            path.stat().st_size == item["bytes"],
            f"manifest: size mismatch for {relative}",
        )

    return manifest


def main():
    for path in (MANIFEST_PATH,):
        if not path.exists():
            print("validate: ERROR: run scripts/build.py first", file=sys.stderr)
            sys.exit(1)

    validate_layout()
    manifest = validate_manifest()
    dimension_total = validate_dimensions(manifest)
    reference = validate_references(manifest)
    validate_csv(dimension_total, reference)
    validate_sqlite(dimension_total, reference)

    if errors:
        print(f"validate: {len(errors)} error(s)", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        sys.exit(1)

    print("validate: OK")
    print(f"  standards        : {manifest['counts']['standards']}")
    print(f"  dimension rows   : {dimension_total}")
    print(f"  reference topics : {reference['topics']}")
    print(f"  reference tables : {reference['tables']} ({reference['rows']} rows, {reference['cells']} cells)")
    print(f"  manifest files   : {len(manifest['files'])}")


if __name__ == "__main__":
    main()
