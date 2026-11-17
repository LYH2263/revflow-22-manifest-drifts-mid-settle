from __future__ import annotations

import json
from pathlib import Path

from app.db import connect, fixtures_dir, table_columns, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  manifest_count INTEGER NOT NULL DEFAULT 0,
  outside_arrival_count INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settle_manifests (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL UNIQUE REFERENCES settle_runs(id),
  status TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  finished_at TEXT,
  member_count INTEGER NOT NULL DEFAULT 0,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  outside_arrival_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settle_manifest_items (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL REFERENCES settle_manifests(settle_run_id),
  entry_id INTEGER NOT NULL,
  entry_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  target_entry_key TEXT,
  biz_ref TEXT NOT NULL DEFAULT '',
  booked_at TEXT NOT NULL,
  result TEXT,
  result_reason TEXT NOT NULL DEFAULT '',
  UNIQUE(settle_run_id, entry_id),
  UNIQUE(settle_run_id, entry_key)
);
CREATE INDEX IF NOT EXISTS idx_manifest_items_run ON settle_manifest_items(settle_run_id);
CREATE TABLE IF NOT EXISTS settle_manifest_exclusions (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL REFERENCES settle_manifests(settle_run_id),
  entry_id INTEGER NOT NULL,
  entry_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  booked_at TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  UNIQUE(settle_run_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_exclusions_run ON settle_manifest_exclusions(settle_run_id);
CREATE TABLE IF NOT EXISTS settle_manifest_proofs (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL UNIQUE REFERENCES settle_runs(id),
  status TEXT NOT NULL,
  frozen_keys_json TEXT NOT NULL,
  settled_keys_json TEXT NOT NULL,
  drift_keys_json TEXT NOT NULL,
  quarantine_keys_json TEXT NOT NULL,
  frozen_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  drift_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  containment_ok INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manifest_proofs_run ON settle_manifest_proofs(settle_run_id);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 清单成员行冻结后只允许回写结果列；键/金额等摘录不可改，也不可删。
MANIFEST_IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_manifest_items_no_update
BEFORE UPDATE ON settle_manifest_items
WHEN NEW.settle_run_id <> OLD.settle_run_id
  OR NEW.entry_id <> OLD.entry_id
  OR NEW.entry_key <> OLD.entry_key
  OR NEW.kind <> OLD.kind
  OR NEW.account_id <> OLD.account_id
  OR NEW.amount_cents <> OLD.amount_cents
  OR COALESCE(NEW.target_entry_key, '') <> COALESCE(OLD.target_entry_key, '')
  OR NEW.biz_ref <> OLD.biz_ref
  OR NEW.booked_at <> OLD.booked_at
BEGIN
  SELECT RAISE(ABORT, '消费清单成员行不可变');
END;
CREATE TRIGGER IF NOT EXISTS trg_manifest_items_no_delete
BEFORE DELETE ON settle_manifest_items
BEGIN
  SELECT RAISE(ABORT, '消费清单成员行不可删');
END;
"""

# 清单对照证明一经写入即归档证据：四组键集合与收口结论不可改、不可删。
PROOF_IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_manifest_proofs_no_update
BEFORE UPDATE ON settle_manifest_proofs
BEGIN
  SELECT RAISE(ABORT, '清单对照证明不可变');
END;
CREATE TRIGGER IF NOT EXISTS trg_manifest_proofs_no_delete
BEFORE DELETE ON settle_manifest_proofs
BEGIN
  SELECT RAISE(ABORT, '清单对照证明不可删');
END;
"""

# 旧库补列（ALTER TABLE 无 IF NOT EXISTS，用 PRAGMA 守卫）。
MIGRATED_COLUMNS = {
    "settle_runs": [
        ("manifest_count", "INTEGER NOT NULL DEFAULT 0"),
        ("outside_arrival_count", "INTEGER NOT NULL DEFAULT 0"),
        ("failure_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "verify_reports": [
        ("manifest_count", "INTEGER NOT NULL DEFAULT 0"),
        ("manifest_settled_count", "INTEGER NOT NULL DEFAULT 0"),
        ("manifest_quarantine_count", "INTEGER NOT NULL DEFAULT 0"),
        ("outside_arrival_count", "INTEGER NOT NULL DEFAULT 0"),
        ("manifest_consistent", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _migrate_schema(conn) -> None:
    for table, columns in MIGRATED_COLUMNS.items():
        have = table_columns(conn, table)
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.executescript(MANIFEST_IMMUTABLE_TRIGGERS)
        conn.executescript(PROOF_IMMUTABLE_TRIGGERS)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = fixtures_dir() / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        with transaction(conn):
            _seed(conn, data)


def _seed(conn, data: dict) -> None:
    for a in data["accounts"]:
        conn.execute(
            "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
            (a["id"], a["code"], a["name"], a["currency"]),
        )
    for e in data["forward_entries"]:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
            """,
            (
                e["id"],
                e["entry_key"],
                e["account_id"],
                e["amount_cents"],
                e.get("biz_ref", ""),
                e["booked_at"],
                e.get("note", ""),
            ),
        )
    # 冲正 id 与正向可能重叠，用独立自增：先取 max
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
    for i, e in enumerate(data["reversal_entries"], start=1):
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
            """,
            (
                max_id + i,
                e["entry_key"],
                e["account_id"],
                e["amount_cents"],
                e.get("biz_ref", ""),
                e.get("target_entry_key"),
                e["booked_at"],
                e.get("note", ""),
            ),
        )
