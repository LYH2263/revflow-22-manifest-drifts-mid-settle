"""归并整单失败闭：只消费清单成员，任一完整性校验失败则整趟回滚。

`balance_run` 必须在调用方的 `BEGIN IMMEDIATE` 事务内执行，且只读
`settle_manifest_items` 的快照金额；正式余额与流水状态同事务提交。
"""

from __future__ import annotations

import sqlite3

from app.modules.settle_manifest import (
    MANIFEST_BALANCED,
    MANIFEST_FAILED,
    MANIFEST_FROZEN,
    PENDING,
    RUN_DONE,
    RUN_FAILED,
    get_manifest,
    now_iso,
    record_exclusions,
)

SETTLED = "已归并"
QUARANTINED = "已隔离"


class ManifestIntegrityError(Exception):
    """清单成员与库内流水不一致（被改动/状态漂移）。"""


def _integrity_check(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    members = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM settle_manifest_items WHERE settle_run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    for m in members:
        if m["result"] is not None:
            raise ManifestIntegrityError(f"清单成员 {m['entry_key']} 已有结果，拒绝重复配平")
        live = conn.execute(
            """
            SELECT entry_key, kind, account_id, amount_cents, target_entry_key,
                   status, settle_run_id
            FROM ledger_entries WHERE id = ?
            """,
            (m["entry_id"],),
        ).fetchone()
        if live is None:
            raise ManifestIntegrityError(f"清单成员 {m['entry_key']} 在流水中已不存在")
        drift = []
        if live["entry_key"] != m["entry_key"]:
            drift.append("流水键")
        if live["kind"] != m["kind"]:
            drift.append("方向")
        if live["account_id"] != m["account_id"]:
            drift.append("账户")
        if live["amount_cents"] != m["amount_cents"]:
            drift.append("金额")
        if (live["target_entry_key"] or "") != (m["target_entry_key"] or ""):
            drift.append("原单键")
        if drift:
            raise ManifestIntegrityError(
                f"清单成员 {m['entry_key']} 的{'/'.join(drift)}在固化后被改动"
            )
        if live["status"] != PENDING or live["settle_run_id"] is not None:
            raise ManifestIntegrityError(
                f"清单成员 {m['entry_key']} 已不在待归并状态（{live['status']}）"
            )
    return members


def _apply_status(
    conn: sqlite3.Connection,
    run_id: int,
    member: dict,
    status: str,
    reason: str,
) -> None:
    cur = conn.execute(
        """
        UPDATE ledger_entries SET status = ?, settle_run_id = ?
        WHERE id = ? AND status = ? AND settle_run_id IS NULL
        """,
        (status, run_id, member["entry_id"], PENDING),
    )
    if cur.rowcount != 1:
        raise ManifestIntegrityError(f"清单成员 {member['entry_key']} 状态写入未命中")
    conn.execute(
        "UPDATE settle_manifest_items SET result = ?, result_reason = ? WHERE id = ?",
        (status, reason, member["id"]),
    )


def _quarantine(conn: sqlite3.Connection, run_id: int, member: dict, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason, amount_cents, settle_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            member["entry_key"],
            member["account_id"],
            reason,
            member["amount_cents"],
            run_id,
            now_iso(),
        ),
    )
    _apply_status(conn, run_id, member, QUARANTINED, reason)


def balance_run(conn: sqlite3.Connection, run_id: int, recorded_at: str | None = None) -> dict:
    """按固化清单配平：只动清单成员，余额按净额增量写。"""
    manifest = get_manifest(conn, run_id)

    pending = [
        dict(r)
        for r in conn.execute(
            "SELECT id, entry_key, account_id, kind, amount_cents, biz_ref, "
            "target_entry_key, booked_at FROM ledger_entries "
            "WHERE status = '待归并' AND kind IN ('正向', '冲正') ORDER BY id"
        ).fetchall()
    ]
    members = pending
    forwards = {m["entry_key"]: m for m in members if m["kind"] == "正向"}
    reversals = [m for m in members if m["kind"] == "冲正"]



    account_net: dict[int, int] = {}
    used: dict[str, int] = {key: 0 for key in forwards}
    settled_count = 0
    quarantine_count = 0

    for m in members:
        if m["kind"] == "正向":
            account_net[m["account_id"]] = account_net.get(m["account_id"], 0) + m["amount_cents"]

    for m in reversals:
        target_key = m["target_entry_key"]
        if not target_key:
            _quarantine(conn, run_id, m, "冲正无原单")
            quarantine_count += 1
            continue
        target = forwards.get(target_key)
        if target is None:
            in_ledger = conn.execute(
                "SELECT 1 FROM ledger_entries WHERE entry_key = ?", (target_key,)
            ).fetchone()
            reason = "原单不在本清单" if in_ledger else "冲正无原单"
            _quarantine(conn, run_id, m, reason)
            quarantine_count += 1
            continue
        if target["account_id"] != m["account_id"]:
            _quarantine(conn, run_id, m, "冲正账户与原单不一致")
            quarantine_count += 1
            continue
        if m["amount_cents"] > target["amount_cents"] - used[target_key]:
            _quarantine(conn, run_id, m, "冲正超额")
            quarantine_count += 1
            continue

        used[target_key] += m["amount_cents"]
        account_net[m["account_id"]] = account_net.get(m["account_id"], 0) - m["amount_cents"]
        _apply_status(conn, run_id, m, SETTLED, "冲正配平")
        settled_count += 1

    for key, m in forwards.items():
        _apply_status(conn, run_id, m, SETTLED, "正向落账")
        settled_count += 1

    net_total = 0
    for account_id, net in account_net.items():
        conn.execute(
            """
            UPDATE accounts
            SET balance_cents = balance_cents + ?, last_settle_run_id = ?
            WHERE id = ?
            """,
            (net, run_id, account_id),
        )
        net_total += net

    outside_count = conn.execute(
        "SELECT COUNT(*) AS c FROM settle_manifest_exclusions WHERE settle_run_id = ?",
        (run_id,),
    ).fetchone()["c"]

    finished = recorded_at or now_iso()
    conn.execute(
        """
        UPDATE settle_runs
        SET status = ?, settled_count = ?, quarantine_count = ?,
            net_cents = ?, outside_arrival_count = ?, finished_at = ?
        WHERE id = ?
        """,
        (RUN_DONE, settled_count, quarantine_count, net_total, outside_count, finished, run_id),
    )
    conn.execute(
        """
        UPDATE settle_manifests
        SET status = ?, settled_count = ?, quarantine_count = ?,
            outside_arrival_count = ?, net_cents = ?, finished_at = ?
        WHERE settle_run_id = ?
        """,
        (
            MANIFEST_BALANCED,
            settled_count,
            quarantine_count,
            outside_count,
            net_total,
            finished,
            run_id,
        ),
    )
    return {
        "settled_count": settled_count,
        "quarantine_count": quarantine_count,
        "outside_arrival_count": outside_count,
        "net_cents": net_total,
    }


def mark_failed(conn: sqlite3.Connection, run_id: int, reason: str) -> None:
    """失败闭：清单与运行标记失败；余额与流水保持原样。"""
    text = reason[:500]
    conn.execute(
        """
        UPDATE settle_runs SET status = ?, finished_at = ?, failure_reason = ?
        WHERE id = ?
        """,
        (RUN_FAILED, now_iso(), text, run_id),
    )
    conn.execute(
        """
        UPDATE settle_manifests
        SET status = ?, failure_reason = ?, finished_at = ?
        WHERE settle_run_id = ?
        """,
        (MANIFEST_FAILED, text, now_iso(), run_id),
    )
