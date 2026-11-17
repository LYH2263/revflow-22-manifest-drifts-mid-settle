"""归并清单对照证明（manifest attestation）：每趟归并运行一份，挂归并运行 id。

证明实体在配平事务内同库写入，四个键集合与本趟清单、流水、隔离同源：

- 固化键：开跑瞬间钉扎进 ``settle_manifest_items`` 的候选冲正/正向键集合，
  本趟只允许处理该集合；
- 实际配平键：本趟真正写进正式余额（状态置「已归并」）的流水键；
- 漂移键：配平时仍处待归并、却不在本趟固化清单内的清单外键（迟到流水），
  它们不得进入本趟正式配平，留待后续趟；
- 隔离键：本趟清单成员中未能配平、落入隔离区的键。

配平成功出「已配平」证明；失败闭出「失败」证明，此时实际配平键/隔离键为空，
漂移键仍照常固化，证明失败当趟正式余额与流水状态未动。
"""

from __future__ import annotations

import json
import sqlite3

from app.modules.settle_manifest import (
    MANIFEST_BALANCED,
    MANIFEST_FAILED,
    PENDING,
    now_iso,
)

PROOF_BALANCED = MANIFEST_BALANCED
PROOF_FAILED = MANIFEST_FAILED


class ProofNotFound(Exception):
    pass


def _load_key_list(blob: str) -> list[str]:
    if not blob:
        return []
    return json.loads(blob)


def write_proof(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    frozen_keys: list[str],
    settled_keys: list[str],
    drift_keys: list[str],
    quarantine_keys: list[str],
    containment_ok: bool,
    failure_reason: str = "",
) -> dict:
    """在调用方事务内写本趟清单对照证明（每趟唯一，已存在则拒绝重复写）。"""
    exists = conn.execute(
        "SELECT 1 FROM settle_manifest_proofs WHERE settle_run_id = ?", (run_id,)
    ).fetchone()
    if exists:
        raise RuntimeError(f"归并运行 #{run_id} 的清单对照证明已存在，不可覆盖")
    cur = conn.execute(
        """
        INSERT INTO settle_manifest_proofs(
          settle_run_id, status, frozen_keys_json, settled_keys_json,
          drift_keys_json, quarantine_keys_json, frozen_count, settled_count,
          drift_count, quarantine_count, containment_ok, failure_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            status,
            json.dumps(frozen_keys, ensure_ascii=False),
            json.dumps(settled_keys, ensure_ascii=False),
            json.dumps(drift_keys, ensure_ascii=False),
            json.dumps(quarantine_keys, ensure_ascii=False),
            len(frozen_keys),
            len(settled_keys),
            len(drift_keys),
            len(quarantine_keys),
            1 if containment_ok else 0,
            failure_reason[:500],
            now_iso(),
        ),
    )
    return get_proof(conn, run_id, proof_id=cur.lastrowid)


def failure_proof(conn: sqlite3.Connection, run_id: int, reason: str) -> dict | None:
    """失败闭证明：固化键照快照；实际配平/隔离键为空（事务已回滚，正式数据未动）。

    漂移键按失败当刻库内状态现查——这些清单外键本就不属本趟，不参与回滚。
    已存在证明（理论上失败趟不会有）时返回 None，不覆盖。
    """
    head = conn.execute(
        "SELECT 1 FROM settle_manifests WHERE settle_run_id = ?", (run_id,)
    ).fetchone()
    if head is None:
        return None
    if conn.execute(
        "SELECT 1 FROM settle_manifest_proofs WHERE settle_run_id = ?", (run_id,)
    ).fetchone():
        return None
    frozen_keys = [
        r["entry_key"]
        for r in conn.execute(
            "SELECT entry_key FROM settle_manifest_items WHERE settle_run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    drift_keys = [
        r["entry_key"]
        for r in conn.execute(
            """
            SELECT e.entry_key FROM ledger_entries e
            WHERE e.status = ?
              AND NOT EXISTS (
                SELECT 1 FROM settle_manifest_items m
                WHERE m.settle_run_id = ? AND m.entry_id = e.id
              )
            ORDER BY e.id
            """,
            (PENDING, run_id),
        ).fetchall()
    ]
    return write_proof(
        conn,
        run_id,
        status=PROOF_FAILED,
        frozen_keys=frozen_keys,
        settled_keys=[],
        drift_keys=drift_keys,
        quarantine_keys=[],
        containment_ok=True,
        failure_reason=reason,
    )


def get_proof(
    conn: sqlite3.Connection, run_id: int, proof_id: int | None = None
) -> dict:
    row = conn.execute(
        "SELECT * FROM settle_manifest_proofs WHERE settle_run_id = ?", (run_id,)
    ).fetchone() if proof_id is None else conn.execute(
        "SELECT * FROM settle_manifest_proofs WHERE id = ?", (proof_id,)
    ).fetchone()
    if row is None:
        raise ProofNotFound(f"归并运行 #{run_id} 无清单对照证明")
    out = dict(row)
    for col in ("frozen_keys_json", "settled_keys_json", "drift_keys_json", "quarantine_keys_json"):
        out[col.removesuffix("_json")] = _load_key_list(out[col])
    return out
