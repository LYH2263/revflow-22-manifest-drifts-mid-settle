"""归并整单失败闭：只消费固化清单成员，任一完整性/收口校验失败则整趟回滚。

清单固化（路径 A：开跑 BEGIN 时冻结清单）
-----------------------------------------
`freeze_manifest` 在调用方的 `BEGIN IMMEDIATE` 事务内把开跑瞬间的待归并候选
键集合钉死为 ``settle_manifest_items`` 成员行。本模块配平时**只读这张清单
快照表**取候选，不再回查 ``ledger_entries`` 的待归并集合——因此配平期间装载
线程新写入的清单外流水对本趟不可见，不可能被悄悄配平进正式余额。

漂移收口（提交前子集校验，路径 B，作为路径 A 的纵深防线）
----------------------------------------------------------
即便快照读取被未来改动绕过，提交前仍强制校验「本趟实际触及键 ⊆ 固化清单」：
- 置为已归并/已隔离的流水键必须全部是清单成员；
- 本趟写入的隔离项、触及余额的账户所对应流水也必须落在清单内。
任一越界即抛 `ContainmentViolation`，由调用方整趟 ROLLBACK，本趟余额/状态
全部回滚，清单与运行标记失败并出失败证明。清单外的待归并键只登记为漂移键
（``settle_manifest_exclusions`` + 对照证明），保持待归并留待后续趟。
"""

from __future__ import annotations

import sqlite3

from app.modules import manifest_proof
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


class ContainmentViolation(Exception):
    """实际触及键超出固化清单集合（候选漂移收口失败），整趟必须回滚。"""


def _integrity_check(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """逐条核对清单快照与库内流水：键/方向/账户/金额/原单不可变，状态须仍待归并。"""
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


def _containment_check(
    conn: sqlite3.Connection, run_id: int, member_ids: set[int], member_keys: set[str]
) -> None:
    """路径 B 收口：本趟实际触及的流水键必须 ⊆ 固化清单，否则整趟失败回滚。"""
    outside_ledgers = [
        r["entry_key"]
        for r in conn.execute(
            """
            SELECT e.entry_key FROM ledger_entries e
            WHERE e.settle_run_id = ?
              AND e.id NOT IN (SELECT entry_id FROM settle_manifest_items WHERE settle_run_id = ?)
            """,
            (run_id, run_id),
        ).fetchall()
    ]
    if outside_ledgers:
        raise ContainmentViolation(
            "本趟触及清单外流水键，收口失败：" + "、".join(outside_ledgers)
        )
    outside_quarantine = [
        r["entry_key"]
        for r in conn.execute(
            """
            SELECT entry_key FROM quarantine_items
            WHERE settle_run_id = ?
              AND entry_key NOT IN (
                SELECT entry_key FROM settle_manifest_items WHERE settle_run_id = ?
              )
            """,
            (run_id, run_id),
        ).fetchall()
    ]
    if outside_quarantine:
        raise ContainmentViolation(
            "本趟隔离项含清单外键，收口失败：" + "、".join(outside_quarantine)
        )
    # 被本趟标记结果的成员必须与固化成员一一对应，无遗漏、无越界。
    touched_ids = {
        r["id"]
        for r in conn.execute(
            """
            SELECT e.id FROM ledger_entries e
            WHERE e.settle_run_id = ? AND e.id IN (
              SELECT entry_id FROM settle_manifest_items WHERE settle_run_id = ?
            )
            """,
            (run_id, run_id),
        ).fetchall()
    }
    if touched_ids != member_ids:
        missing = member_keys - {
            r["entry_key"]
            for r in conn.execute(
                """
                SELECT m.entry_key FROM settle_manifest_items m
                JOIN ledger_entries e ON e.id = m.entry_id
                WHERE m.settle_run_id = ? AND e.settle_run_id = ?
                """,
                (run_id, run_id),
            ).fetchall()
        }
        raise ContainmentViolation(
            "本趟实际触及成员与固化清单不一致，未收口键：" + "、".join(sorted(missing))
        )


def balance_run(conn: sqlite3.Connection, run_id: int, recorded_at: str | None = None) -> dict:
    """按固化清单配平（路径 A）：候选只从清单快照读取，余额按净额增量写。

    全程在调用方 ``BEGIN IMMEDIATE`` 事务内；任何异常都由调用方闭并回滚，
    正式余额与流水状态不会留下半截写入。
    """
    head = get_manifest(conn, run_id)
    if head["status"] != MANIFEST_FROZEN:
        raise ManifestIntegrityError(
            f"归并运行 #{run_id} 清单状态为「{head['status']}」，仅「{MANIFEST_FROZEN}」可配平"
        )

    # 1) 完整性：清单快照逐字段比对库内流水，拒绝固化后被改动/抢跑的成员。
    members = _integrity_check(conn, run_id)

    # 2) 漂移登记先于成员状态变更：此刻仍待归并且不在清单内的键即清单外到达，
    #    只登记、保持待归并，绝不进入本趟配平。
    record_exclusions(conn, run_id)

    # 3) 候选集合只取自固化清单快照（路径 A 的核心：不回查 ledger_entries 待归并集）。
    forwards = {m["entry_key"]: m for m in members if m["kind"] == "正向"}
    reversals = [m for m in members if m["kind"] == "冲正"]

    account_net: dict[int, int] = {}
    used: dict[str, int] = {key: 0 for key in forwards}
    settled_keys: list[str] = []
    quarantine_keys: list[str] = []
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
            quarantine_keys.append(m["entry_key"])
            continue
        target = forwards.get(target_key)
        if target is None:
            in_ledger = conn.execute(
                "SELECT 1 FROM ledger_entries WHERE entry_key = ?", (target_key,)
            ).fetchone()
            reason = "原单不在本清单" if in_ledger else "冲正无原单"
            _quarantine(conn, run_id, m, reason)
            quarantine_count += 1
            quarantine_keys.append(m["entry_key"])
            continue
        if target["account_id"] != m["account_id"]:
            _quarantine(conn, run_id, m, "冲正账户与原单不一致")
            quarantine_count += 1
            quarantine_keys.append(m["entry_key"])
            continue
        if m["amount_cents"] > target["amount_cents"] - used[target_key]:
            _quarantine(conn, run_id, m, "冲正超额")
            quarantine_count += 1
            quarantine_keys.append(m["entry_key"])
            continue

        used[target_key] += m["amount_cents"]
        account_net[m["account_id"]] = account_net.get(m["account_id"], 0) - m["amount_cents"]
        _apply_status(conn, run_id, m, SETTLED, "冲正配平")
        settled_count += 1
        settled_keys.append(m["entry_key"])

    for key, m in forwards.items():
        _apply_status(conn, run_id, m, SETTLED, "正向落账")
        settled_count += 1
        settled_keys.append(key)

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

    # 4) 路径 B 收口：提交前校验实际触及键 ⊆ 固化清单，越界即抛错整趟回滚。
    member_ids = {m["entry_id"] for m in members}
    member_keys = {m["entry_key"] for m in members}
    _containment_check(conn, run_id, member_ids, member_keys)

    drift_rows = conn.execute(
        "SELECT entry_key FROM settle_manifest_exclusions WHERE settle_run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    drift_keys = [r["entry_key"] for r in drift_rows]
    outside_count = len(drift_keys)

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

    # 5) 清单对照证明与本趟余额/状态同事务落库，四组键集合与清单、流水、隔离同源。
    manifest_proof.write_proof(
        conn,
        run_id,
        status=manifest_proof.PROOF_BALANCED,
        frozen_keys=[m["entry_key"] for m in members],
        settled_keys=settled_keys,
        drift_keys=drift_keys,
        quarantine_keys=quarantine_keys,
        containment_ok=True,
    )
    return {
        "settled_count": settled_count,
        "quarantine_count": quarantine_count,
        "outside_arrival_count": outside_count,
        "net_cents": net_total,
    }


def mark_failed(conn: sqlite3.Connection, run_id: int, reason: str) -> None:
    """失败闭：清单与运行标记失败；余额与流水保持原样，并出失败对照证明。"""
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
    # 失败证明：实际配平/隔离键为空（配平事务已回滚），漂移键照当刻库态固化。
    manifest_proof.failure_proof(conn, run_id, text)
