from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts, transaction
from app.modules import fail_closed_settle, settle_manifest as manifest


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        frozen = conn.execute(
            "SELECT settle_run_id FROM settle_manifests WHERE status = ?",
            (manifest.MANIFEST_FROZEN,),
        ).fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "待配平清单": frozen["settle_run_id"] if frozen else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, m.status AS manifest_status
            FROM settle_runs r
            LEFT JOIN settle_manifests m ON m.settle_run_id = r.id
            ORDER BY r.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def _open_run(conn, now: str) -> int:
    """建运行并固化清单（调用方保证处于 BEGIN IMMEDIATE 内）。"""
    frozen = conn.execute(
        "SELECT settle_run_id FROM settle_manifests WHERE status = ?",
        (manifest.MANIFEST_FROZEN,),
    ).fetchone()
    if frozen:
        raise HTTPException(
            409,
            {
                "消息": f"归并运行 #{frozen['settle_run_id']} 的消费清单已固化尚未配平，请先配平该清单",
                "待配平运行": frozen["settle_run_id"],
            },
        )
    cur = conn.execute(
        "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)", (now,)
    )
    run_id = cur.lastrowid
    manifest.freeze_manifest(conn, run_id)
    return run_id


def freeze_run(freeze_only: bool = False) -> dict:
    """事务 A：固化本趟消费清单；freeze_only 时停在「清单已固化」。"""
    now = _now()
    conn = connect()
    try:
        with transaction(conn):
            run_id = _open_run(conn, now)
        if not freeze_only:
            return _balance_or_fail(run_id)
        return get_settle_run(run_id)
    finally:
        conn.close()


def _balance_or_fail(run_id: int) -> dict:
    """事务 B：只消费清单成员；失败则整趟回滚并把清单/运行标失败。"""
    conn = connect()
    try:
        try:
            with transaction(conn):
                fail_closed_settle.balance_run(conn, run_id)
        except Exception as exc:  # 失败闭：余额与流水未动，清单标记失败
            detail = str(exc) or exc.__class__.__name__
            try:
                with transaction(conn):
                    fail_closed_settle.mark_failed(conn, run_id, detail)
            except Exception:  # 标记失败本身失败也不影响「正式余额未动」
                pass
            raise HTTPException(
                409,
                {"消息": f"归并运行 #{run_id} 配平失败，余额未变", "运行": run_id, "原因": detail},
            ) from exc
        return get_settle_run(run_id)
    finally:
        conn.close()


def balance_frozen_run(run_id: int) -> dict:
    """对已固化清单执行配平；只允许「清单已固化」状态。"""
    conn = connect()
    try:
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")
        try:
            frozen = manifest.get_manifest(conn, run_id)
        except manifest.ManifestNotFound:
            raise HTTPException(409, f"归并运行 #{run_id} 无消费清单，不能配平") from None
        if frozen["status"] != manifest.MANIFEST_FROZEN:
            raise HTTPException(
                409,
                f"归并运行 #{run_id} 清单状态为「{frozen['status']}」，仅「{manifest.MANIFEST_FROZEN}」可配平",
            )
    finally:
        conn.close()
    return _balance_or_fail(run_id)


def run_settle(freeze_only: bool = False) -> dict:
    """固化清单 → 事务配平；清单一旦固化，本趟成员集合不再受新装载影响。"""
    return freeze_run(freeze_only=freeze_only)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT r.*, m.status AS manifest_status, m.frozen_at
            FROM settle_runs r
            LEFT JOIN settle_manifests m ON m.settle_run_id = r.id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def get_manifest_view(run_id: int) -> dict:
    with connect() as conn:
        with transaction(conn):
            run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, "归并运行不存在")
            try:
                view = manifest.manifest_view(conn, run_id)
            except manifest.ManifestNotFound:
                raise HTTPException(404, "该归并运行无消费清单") from None
            view["run"] = dict(run)
            return view


def run_verify(settle_run_id: int | None = None) -> dict:
    """同源互证：清单成员/结果/流水/余额在同一读事务内核对并出报告。"""
    conn = connect()
    try:
        with transaction(conn):
            if settle_run_id is None:
                last = conn.execute(
                    "SELECT id FROM settle_runs WHERE status = ? ORDER BY id DESC LIMIT 1",
                    (manifest.RUN_DONE,),
                ).fetchone()
                if not last:
                    raise HTTPException(400, "尚无可互证的已完成归并运行")
                settle_run_id = last["id"]
            run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
            if not run:
                raise HTTPException(404, "归并运行不存在")
            if run["status"] != manifest.RUN_DONE:
                raise HTTPException(
                    409,
                    f"归并运行 #{settle_run_id} 状态为「{run['status']}」，只有已完成的归并对账可出互证",
                )
            head = manifest.get_manifest(conn, settle_run_id)
            members = manifest.manifest_items(conn, settle_run_id)
            exclusions = manifest.exclusion_rows(conn, settle_run_id)

            settled_rows = [m for m in members if m["result"] == "已归并"]
            quarantined_rows = [m for m in members if m["result"] == "已隔离"]
            unexecuted_rows = [m for m in members if m["result"] not in ("已归并", "已隔离")]

            # 成员结果 vs 实时流水状态逐一核对
            mismatched = []
            for m in members:
                live = conn.execute(
                    "SELECT status, settle_run_id FROM ledger_entries WHERE id = ?",
                    (m["entry_id"],),
                ).fetchone()
                if (
                    live is None
                    or live["status"] != m["result"]
                    or live["settle_run_id"] != settle_run_id
                ):
                    mismatched.append(m["entry_key"])

            # 清单头与运行行的计数必须一致
            header_counts_match = (
                head["member_count"] == len(members)
                and head["settled_count"] == len(settled_rows)
                and head["quarantine_count"] == len(quarantined_rows)
                and head["outside_arrival_count"] == len(exclusions)
                and run["manifest_count"] == len(members)
                and run["settled_count"] == len(settled_rows)
                and run["quarantine_count"] == len(quarantined_rows)
                and run["outside_arrival_count"] == len(exclusions)
                and len(members) == len(settled_rows) + len(quarantined_rows) + len(unexecuted_rows)
            )

            member_net = sum(
                m["amount_cents"] if m["kind"] == "正向" else -m["amount_cents"]
                for m in settled_rows
            )

            fwd_sum = conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
                WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
                """,
                (settle_run_id,),
            ).fetchone()["s"]
            rev_sum = conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
                WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
                """,
                (settle_run_id,),
            ).fetchone()["s"]
            ledger_net = fwd_sum - rev_sum
            balance_sum = conn.execute(
                "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
                (settle_run_id,),
            ).fetchone()["s"]
            all_bal = conn.execute(
                "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
            ).fetchone()["s"]
            q_count = conn.execute(
                "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
                (settle_run_id,),
            ).fetchone()["c"]

            all_fwd = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
            ).fetchone()["s"]
            all_rev = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
            ).fetchone()["s"]
            all_ledger_net = all_fwd - all_rev

            # 清单对账：成员数 = 成功 + 隔离 + 未执行（未执行必须为 0），
            # 成员结果与流水状态一一对应，清单头/运行行计数一致，且清单净额自洽。
            manifest_consistent = (
                header_counts_match
                and not mismatched
                and not unexecuted_rows
                and member_net == head["net_cents"]
                and ledger_net == head["net_cents"]
            )
            balance_consistent = all_bal == all_ledger_net
            consistent = 1 if (manifest_consistent and balance_consistent) else 0
            detail = {
                "清单成员数": len(members),
                "成功数": len(settled_rows),
                "隔离数": len(quarantined_rows),
                "未执行数": len(unexecuted_rows),
                "清单外新到达笔数": len(exclusions),
                "清单净额分": head["net_cents"],
                "清单成员净额分": member_net,
                "本轮正向分": fwd_sum,
                "本轮冲正分": rev_sum,
                "本轮流水净额分": ledger_net,
                "本轮触及余额合计分": balance_sum,
                "全量流水净额分": all_ledger_net,
                "全量账户余额分": all_bal,
                "本轮隔离数": q_count,
                "清单头计数一致": header_counts_match,
                "清单对账一致": manifest_consistent,
                "余额互证一致": balance_consistent,
                "成员结果与流水不一致键": mismatched,
                "未执行成员键": [m["entry_key"] for m in unexecuted_rows],
                "清单外新到达键": [x["entry_key"] for x in exclusions],
                "隔离成员键": [m["entry_key"] for m in quarantined_rows],
            }
            now = _now()
            cur = conn.execute(
                """
                INSERT INTO verify_reports(
                  settle_run_id, ledger_net_cents, balance_sum_cents,
                  quarantine_count, consistent, detail_json, created_at,
                  manifest_count, manifest_settled_count, manifest_quarantine_count,
                  outside_arrival_count, manifest_consistent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settle_run_id,
                    all_ledger_net,
                    all_bal,
                    q_count,
                    consistent,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                    len(members),
                    len(settled_rows),
                    len(quarantined_rows),
                    len(exclusions),
                    1 if manifest_consistent else 0,
                ),
            )
            rid = cur.lastrowid
            row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
            out = dict(row)
            out["detail"] = detail
            return out
    finally:
        conn.close()
