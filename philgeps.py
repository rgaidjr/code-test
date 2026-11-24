"""Read-only SQL tools over a local, rebuildable index of the PhilGEPS CSV."""

import argparse
import csv
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
VERSION = "1"
# Original headers, SQL names, and conversion types. Monetary values use integer
# centavos so SUMs remain exact; SQL examples divide by 100.0 for PHP display.
COLUMNS = [
    ("Procuring Entity (PE)", "entity", "TEXT"),
    ("Region", "region", "TEXT"), ("Province", "province", "TEXT"),
    ("City/Municipality", "city", "TEXT"),
    ("Government Branch", "government_branch", "TEXT"),
    ("PE Organization Type", "organization_type", "TEXT"),
    ("PE Organization Type (Grouped)", "organization_group", "TEXT"),
    ("Bid Reference No.", "bid_reference", "TEXT"),
    ("Solicitation No.", "solicitation", "TEXT"),
    ("Notice Title", "title", "TEXT"),
    ("Classification", "classification", "TEXT"),
    ("Procurement Mode", "procurement_mode", "TEXT"),
    ("Procurement Category", "category", "TEXT"),
    ("Source of Funds", "source_of_funds", "TEXT"),
    ("Funding Instrument", "funding_instrument", "TEXT"),
    ("Applicable Procurement Rules", "procurement_rules", "TEXT"),
    ("Approved Budget of the Contract", "approved_budget_cents", "INTEGER"),
    ("Published Date", "published_date", "TEXT"),
    ("Closing Date", "closing_date", "TEXT"),
    ("Area of Delivery/Project Location", "delivery_location", "TEXT"),
    ("Delivery Period", "delivery_period", "TEXT"),
    ("Line Item No", "line_item_number", "TEXT"),
    ("Item Name", "item_name", "TEXT"),
    ("Item Description", "item_description", "TEXT"),
    ("Quantity", "quantity", "TEXT"), ("UOM", "uom", "TEXT"),
    ("Item Budget", "item_budget_cents", "INTEGER"),
    ("Bid Notice Status", "bid_status", "TEXT"),
    ("Award Reference No.", "award_reference", "TEXT"),
    ("Award Title", "award_title", "TEXT"),
    ("UNSPSC Code", "unspsc_code", "TEXT"),
    ("UNSPSC Description", "unspsc_description", "TEXT"),
    ("Published Date(Award)", "award_published_date", "TEXT"),
    ("Award Date", "award_date", "TEXT"),
    ("Contract Amount", "contract_amount_cents", "INTEGER"),
    ("Award Notice Status", "award_status", "TEXT"),
    ("Notice to Proceed Date", "notice_to_proceed_date", "TEXT"),
    ("Contract Effectivity Date", "contract_effectivity_date", "TEXT"),
    ("Contract End Date", "contract_end_date", "TEXT"),
    ("Awardee OrgID", "awardee_id", "TEXT"),
    ("Awardee Organization Name", "awardee", "TEXT"),
    ("Country of Awardee", "awardee_country", "TEXT"),
    ("Region of Awardee", "awardee_region", "TEXT"),
    ("Province of Awardee", "awardee_province", "TEXT"),
    ("City/Municipality of Awardee", "awardee_city", "TEXT"),
    ("Awardee Size", "awardee_size", "TEXT"),
    ("Awardee Joint Venture", "awardee_joint_venture", "TEXT"),
]

VIEWS = """
CREATE TABLE notices AS
SELECT notice_key, MIN(source_row) AS source_row,
       MIN(bid_reference) AS bid_reference, MIN(solicitation) AS solicitation,
       MIN(entity) AS entity, MIN(region) AS region, MIN(province) AS province,
       MIN(title) AS title, MIN(classification) AS classification,
       MIN(procurement_mode) AS procurement_mode, MIN(category) AS category,
       MIN(bid_status) AS bid_status, MIN(published_date) AS published_date,
       MIN(closing_date) AS closing_date,
       CASE WHEN COUNT(DISTINCT approved_budget_cents) = 1
            THEN MIN(approved_budget_cents) END AS approved_budget_cents,
       COUNT(DISTINCT approved_budget_cents) AS budget_value_count,
       COUNT(DISTINCT COALESCE(entity, '') || char(31) || COALESCE(region, '')
             || char(31) || COALESCE(category, '') || char(31)
             || COALESCE(procurement_mode, '') || char(31)
             || COALESCE(published_date, '') || char(31)
             || COALESCE(bid_status, '')) AS metadata_variant_count,
       COUNT(*) AS line_rows
FROM records GROUP BY notice_key;

CREATE TABLE awards AS
SELECT award_key, MIN(source_row) AS source_row,
       MIN(award_reference) AS award_reference, MIN(bid_reference) AS bid_reference,
       MIN(entity) AS entity, MIN(awardee) AS awardee,
       MIN(award_date) AS award_date, MIN(award_title) AS title,
       CASE WHEN COUNT(DISTINCT contract_amount_cents) = 1
            THEN MIN(contract_amount_cents) END AS contract_amount_cents,
       COUNT(DISTINCT contract_amount_cents) AS amount_value_count,
       COUNT(DISTINCT COALESCE(awardee, '')) AS awardee_variant_count,
       COUNT(*) AS line_rows
FROM records WHERE award_reference IS NOT NULL GROUP BY award_key;
CREATE INDEX records_notice ON records(notice_key);
CREATE INDEX records_bid ON records(bid_reference);
CREATE INDEX notices_region ON notices(region);
CREATE INDEX notices_entity ON notices(entity);
"""

NOTES = [
    "Snapshot only, not live PhilGEPS. Dates describe this file's coverage, not all procurement.",
    "records are CSV line-item rows. Never sum notice budgets or contract amounts across records.",
    "notices deduplicates nonzero bid references; absent/0 references use entity, solicitation, title, published date. Missing solicitation falls back to source row. These are analytical identities, not verified unique contracts.",
    "notices uses minimum text values when rows disagree. For grouped rankings use metadata_variant_count=1, and disclose excluded notices. Conflicting budgets become NULL; budget_value_count>1 identifies them.",
    "awards deduplicates award reference plus entity; missing award references are excluded. Conflicting amounts become NULL. Filter awardee_variant_count=1 for supplier rankings.",
    "Amounts ending in _cents are integer centavos. Divide by 100.0 to display PHP. Approved budgets are not actual spending. NULL is unavailable, never zero.",
    "Bid status Awarded does not guarantee populated award details. Supplier results cover only the small populated award subset. Do not infer corruption or savings from this snapshot alone.",
    "source_row is the 1-based CSV data-record number (header excluded), stable while the source file is unchanged. Quoted multiline cells mean it is not a physical text line number.",
]


def source_path():
    return Path(os.getenv("PHILGEPS_CSV", str(ROOT / "DATASETS_PHILGEPS.csv"))).resolve()


def clean(value):
    value = value.strip()
    return None if value.upper() in {"", "NULL", "N/A"} else value


def convert(value, name, kind, issues):
    value = clean(value)
    if value is None:
        return None
    if name in {"bid_reference", "award_reference"} and value == "0":
        return None
    if kind == "INTEGER":
        try:
            amount = Decimal(value.replace(",", "")) * 100
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise ValueError("Amount is not an exact centavo value")
            cents = int(amount)
            if not -(2**63) <= cents < 2**63:
                raise ValueError("Amount exceeds SQLite integer range")
            return cents
        except (InvalidOperation, ValueError):
            issues[name] = issues.get(name, 0) + 1
            return None
    if name.endswith("_date"):
        try:
            return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
        except ValueError:
            issues[name] = issues.get(name, 0) + 1
            return None
    return value


def ensure_database():
    source = source_path()
    stat = source.stat()
    encoding = os.getenv("PHILGEPS_ENCODING", "cp1252")
    signature = json.dumps([VERSION, str(source), stat.st_size, stat.st_mtime_ns, encoding])
    cache = Path(os.getenv("PHILGEPS_CACHE_DIR", str(ROOT / ".cache"))).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / "philgeps.sqlite3"
    if destination.exists():
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone() == (signature,):
                return destination
    # Build separately, then replace atomically. Readers never see a partial index.
    handle, name = tempfile.mkstemp(prefix="philgeps-", suffix=".sqlite3", dir=cache)
    os.close(handle)
    temporary = Path(name)
    issues = {}
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
            declarations = ", ".join(f'"{name}" {kind}' for _, name, kind in COLUMNS)
            db.execute(f"CREATE TABLE records (source_row INTEGER PRIMARY KEY, notice_key TEXT, award_key TEXT, {declarations})")
            placeholders = ",".join("?" for _ in range(len(COLUMNS) + 3))
            insert = f"INSERT INTO records VALUES ({placeholders})"
            batch = []
            with source.open(encoding=encoding, newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != [col[0] for col in COLUMNS]:
                    raise ValueError("CSV headers differ from the supported PhilGEPS schema")
                for number, raw in enumerate(reader, 1):
                    if None in raw or None in raw.values():
                        raise ValueError(f"Malformed CSV data record {number}")
                    values = [convert(raw[header], name, kind, issues) for header, name, kind in COLUMNS]
                    record = dict(zip((c[1] for c in COLUMNS), values))
                    if record["bid_reference"]:
                        notice = ["bid", record["bid_reference"]]
                    elif record["solicitation"]:
                        notice = ["fallback", *[record[k] for k in ("entity", "solicitation", "title", "published_date")]]
                    else:
                        notice = ["row", number]
                    award = [record["entity"], record["award_reference"]] if record["award_reference"] else None
                    batch.append([number, json.dumps(notice, ensure_ascii=True),
                                  json.dumps(award, ensure_ascii=True) if award else None, *values])
                    if len(batch) >= 2000:
                        db.executemany(insert, batch)
                        batch.clear()
                if batch:
                    db.executemany(insert, batch)
            db.executescript(VIEWS)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("signature", signature), ("parse_issues", json.dumps(issues)),
            ])
            db.commit()
        if source.stat().st_mtime_ns != stat.st_mtime_ns or source.stat().st_size != stat.st_size:
            raise ValueError("CSV changed during indexing; retry")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def describe_dataset() -> dict[str, Any]:
    """Describe the PhilGEPS snapshot, SQL tables/columns, coverage and counting caveats.

    Call before query_dataset. Use notices for budget rankings, records for
    line-item evidence, awards for the populated award subset. Amounts are cents.
    """
    path = ensure_database()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        coverage = dict(db.execute("""SELECT COUNT(*) AS raw_rows,
            MIN(published_date) AS first_published, MAX(published_date) AS last_published,
            SUM(contract_amount_cents IS NOT NULL) AS rows_with_contract_amount,
            SUM(bid_reference IS NULL) AS rows_without_bid_reference,
            SUM(bid_status='Awarded') AS rows_marked_awarded FROM records""").fetchone())
        coverage.update(dict(db.execute("""SELECT COUNT(*) AS notices,
            SUM(budget_value_count>1) AS notices_with_conflicting_budgets,
            SUM(metadata_variant_count>1) AS notices_with_conflicting_metadata FROM notices""").fetchone()))
        coverage["awards"] = db.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
        return {
            "source": source_path().name, "coverage": coverage,
            "tables": {table: [dict(row) for row in db.execute(f'PRAGMA table_info("{table}")')]
                       for table in ("records", "notices", "awards")},
            "regions": [r[0] for r in db.execute("SELECT DISTINCT region FROM notices ORDER BY region")],
            "parse_issues": json.loads(db.execute("SELECT value FROM metadata WHERE key='parse_issues'").fetchone()[0]),
            "notes": NOTES,
            "query_limits": {"max_rows": 30, "max_seconds": 5, "max_output_characters": 30000},
        }


def query_dataset(sql: str) -> dict[str, Any]:
    """Run one read-only SQLite SELECT or WITH query over records, notices, awards.

    Call describe_dataset first for schema and caveats. Supports joins, filters,
    CTEs and aggregation. No writes, PRAGMAs, attachments, extensions, or external
    files. Return at most 30 rows. Use ORDER BY for rankings and cite source_row
    or bid_reference for record evidence. Never sum duplicated raw notice budgets.
    """
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 12000:
        raise ValueError("sql must be a nonempty string of at most 12000 characters")
    path = ensure_database()
    allowed_tables = {"records", "notices", "awards"}
    allowed_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def authorize(action, arg1, arg2, database, trigger):
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and arg1 not in allowed_tables:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    started = time.monotonic()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        db.set_authorizer(authorize)
        db.set_progress_handler(lambda: int(time.monotonic() - started > 5), 10000)
        try:
            cursor = db.execute(sql)
            if cursor.description is None:
                raise ValueError("Only queries returning rows are supported")
            columns = [c[0] for c in cursor.description]
            fetched = cursor.fetchmany(31)
            truncated = len(fetched) > 30
            rows = []
            output_size = 0
            cells_truncated = False
            for row in fetched[:30]:
                values = []
                for value in row:
                    if isinstance(value, bytes):
                        raise ValueError("Binary query output is not supported")
                    if isinstance(value, str) and len(value) > 1200:
                        value = value[:1200] + " [truncated]"
                        cells_truncated = True
                    values.append(value)
                output_size += len(json.dumps(values))
                if output_size > 30000:
                    truncated = True
                    break
                rows.append(values)
            return {"status": "ok", "source": source_path().name, "sql": sql,
                    "columns": columns, "rows": rows, "returned_rows": len(rows),
                    "truncated": truncated, "cells_truncated": cells_truncated,
                    "notes": NOTES}
        except sqlite3.Error as exc:
            raise ValueError(f"Query rejected or failed: {exc}") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explore the PhilGEPS CSV without a model")
    parser.add_argument("--sql", help="Run a read-only SQL query; otherwise describe the dataset")
    args = parser.parse_args()
    print(json.dumps(query_dataset(args.sql) if args.sql else describe_dataset(), indent=2, ensure_ascii=True))
