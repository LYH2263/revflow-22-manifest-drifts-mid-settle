"""归并开跑前候选清单固化：把「本趟消费集合」先钉死为持久成员行。

冻结必须在调用方的 `BEGIN IMMEDIATE` 事务内完成：先拿库级写锁，再查询待归并
流水并写清单。装载线程无法在「查询待归并」与「清单提交」之间插入新流水，
因此本趟成员集合可复现。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

PENDING = "待归并"
MANIFEST_FROZEN = "已固化"
MANIFEST_BALANCED = "已配平"
MANIFEST_FAILED = "失败"

RUN_FROZEN = "清单已固化"
RUN_DONE = "完成"
RUN_FAILED = "失败"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ManifestNotFound(Exception):
    pass


class ManifestStateError(Exception):
    pass


def freeze_manifest(conn: sqlite3.Connection, run_id: int) -> dict:
    """在调用方事务内固化本趟清单；返回清单头。

    查询与写入同处一个 BEGIN IMMEDIATE 事务，清单成员数即开跑瞬间的待归并集合。
    """
    rows = conn.execute(
        """
        SELECT id, entry_key, account_id, kind, amount_cents, biz_ref,
               target_entry_key, booked_at
        FROM ledger_entries
        WHERE status = ? AND kind IN ('正向', '冲正')
        ORDER BY id
        """,
        (PENDING,),
    ).fetchall()
    forward_count = sum(1 for r in rows if r["kind"] == "正向")
    reversal_count = sum(1 for r in rows if r["kind"] == "冲正")
    frozen_at = now_iso()

    conn.execute(
        """
        INSERT INTO settle_manifests(
          settle_run_id, status, frozen_at, member_count,
          forward_count, reversal_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, MANIFEST_FROZEN, frozen_at, len(rows), forward_count, reversal_count),
    )
    conn.executemany(
        """
        INSERT INTO settle_manifest_items(
          settle_run_id, entry_id, entry_key, kind, account_id, amount_cents,
          target_entry_key, biz_ref, booked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                r["id"],
                r["entry_key"],
                r["kind"],
                r["account_id"],
                r["amount_cents"],
                r["target_entry_key"],
                r["biz_ref"],
                r["booked_at"],
            )
            for r in rows
        ],
    )
    conn.execute(
        """
        UPDATE settle_runs
        SET status = ?, forward_count = ?, reversal_count = ?, manifest_count = ?
        WHERE id = ?
        """,
        (RUN_FROZEN, forward_count, reversal_count, len(rows), run_id),
    )
    return get_manifest(conn, run_id)


def get_manifest(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM settle_manifests WHERE settle_run_id = ?", (run_id,)
    ).fetchone()
    if not row:
        raise ManifestNotFound(f"归并运行 #{run_id} 无消费清单")
    return dict(row)


def manifest_items(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM settle_manifest_items
        WHERE settle_run_id = ?
        ORDER BY kind DESC, id
        """,
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def exclusion_rows(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM settle_manifest_exclusions
        WHERE settle_run_id = ?
        ORDER BY id
        """,
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_exclusions(conn: sqlite3.Connection, run_id: int) -> int:
    """把当前待归并但不在本清单内的流水记为「清单外新到达」，状态保持待归并。

    必须在更新清单成员状态之前调用，否则本趟刚处理完的成员会混进来。
    """
    detected_at = now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO settle_manifest_exclusions(
          settle_run_id, entry_id, entry_key, kind, amount_cents, booked_at, detected_at
        )
        SELECT ?, e.id, e.entry_key, e.kind, e.amount_cents, e.booked_at, ?
        FROM ledger_entries e
        WHERE e.status = ?
          AND NOT EXISTS (
            SELECT 1 FROM settle_manifest_items m
            WHERE m.settle_run_id = ? AND m.entry_id = e.id
          )
        """,
        (run_id, detected_at, PENDING, run_id),
    )
    return cur.rowcount


def manifest_view(conn: sqlite3.Connection, run_id: int) -> dict:
    """归并台视图：清单头 + 成员结果 + 清单外到达 + 汇总对账。"""
    manifest = get_manifest(conn, run_id)
    members = manifest_items(conn, run_id)
    for m in members:
        live = conn.execute(
            "SELECT status, settle_run_id FROM ledger_entries WHERE id = ?",
            (m["entry_id"],),
        ).fetchone()
        m["live_status"] = live["status"] if live else "已删除"
        m["live_settle_run_id"] = live["settle_run_id"] if live else None
        if m["result"]:
            m["outcome"] = m["result"]
        elif m["live_status"] != PENDING:
            m["outcome"] = f"未执行（现为{m['live_status']}）"
        else:
            m["outcome"] = "未执行"
    exclusions = exclusion_rows(conn, run_id)
    settled = sum(1 for m in members if m["result"] == "已归并")
    quarantined = sum(1 for m in members if m["result"] == "已隔离")
    return {
        "manifest": manifest,
        "members": members,
        "exclusions": exclusions,
        "summary": {
            "清单成员数": len(members),
            "成功数": settled,
            "隔离数": quarantined,
            "未执行数": len(members) - settled - quarantined,
            "清单外新到达笔数": len(exclusions),
            "正向数": sum(1 for m in members if m["kind"] == "正向"),
            "冲正数": sum(1 for m in members if m["kind"] == "冲正"),
        },
    }
