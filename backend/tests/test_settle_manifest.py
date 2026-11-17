from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import connect  # noqa: E402
from app.modules import fail_closed_settle  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每例独立库：环境变量在 TestClient 启动（建表+播种）前生效。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIXTURES_DIR", str(ROOT / "fixtures"))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "app.db"


def _insert_pending(db, *, entry_id, entry_key, kind, account_id, amount_cents, target=None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, ?, ?, '', ?, '2026-11-17T00:00:00Z', '待归并', '')
            """,
            (entry_id, entry_key, account_id, kind, amount_cents, target),
        )


def _balances(db) -> dict[int, int]:
    with sqlite3.connect(db) as conn:
        return {
            row[0]: row[1]
            for row in conn.execute("SELECT id, balance_cents FROM accounts ORDER BY id")
        }


def _entry_status(db, entry_key):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT status, settle_run_id FROM ledger_entries WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()


def test_manifest_persisted_and_report_reconciles(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"

    view = client.get(f"/api/settle-runs/{run['id']}/manifest").json()
    summary = view["summary"]
    assert summary["清单成员数"] == run["manifest_count"] == 9  # 5 正向 + 4 冲正
    assert summary["成功数"] + summary["隔离数"] == summary["清单成员数"]
    assert summary["未执行数"] == 0
    assert summary["清单外新到达笔数"] == 0
    assert summary["成功数"] == run["settled_count"]
    assert summary["隔离数"] == run["quarantine_count"]
    assert all(m["result"] for m in view["members"])
    assert all(m["outcome"] == m["result"] for m in view["members"])

    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["manifest_consistent"] == 1
    assert report["manifest_count"] == summary["清单成员数"]
    assert report["manifest_settled_count"] == summary["成功数"]
    assert report["manifest_quarantine_count"] == summary["隔离数"]
    assert report["outside_arrival_count"] == 0
    assert report["ledger_net_cents"] == report["balance_sum_cents"]
    assert report["detail"]["清单成员数"] == summary["清单成员数"]


def test_late_arrivals_stay_out_of_run(client, db):
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    assert frozen["status"] == "清单已固化"
    run_id = frozen["id"]
    before = _balances(db)
    frozen_head = client.get(f"/api/settle-runs/{run_id}/manifest").json()["manifest"]
    assert frozen_head["status"] == "已固化"
    assert frozen_head["settled_count"] == 0

    # 清单固化后装载线程写入：一条正向 + 以它为原单的冲正
    _insert_pending(
        db, entry_id=901, entry_key="FWD-LATE-001", kind="正向",
        account_id=1, amount_cents=7000,
    )
    _insert_pending(
        db, entry_id=902, entry_key="REV-LATE-001", kind="冲正",
        account_id=1, amount_cents=7000, target="FWD-LATE-001",
    )

    done = client.post(f"/api/settle-runs/{run_id}/balance")
    assert done.status_code == 200, done.text
    done = done.json()
    assert done["status"] == "完成"
    assert done["manifest_count"] == frozen_head["member_count"]
    assert done["outside_arrival_count"] == 2

    # 新到达的两笔仍在待归并，未进入本趟成功集
    assert _entry_status(db, "FWD-LATE-001") == ("待归并", None)
    assert _entry_status(db, "REV-LATE-001") == ("待归并", None)
    view = client.get(f"/api/settle-runs/{run_id}/manifest").json()
    assert {x["entry_key"] for x in view["exclusions"]} == {"FWD-LATE-001", "REV-LATE-001"}
    assert view["summary"]["清单外新到达笔数"] == 2
    # 余额只反映清单内净额
    expected_delta = sum(
        m["amount_cents"] if m["kind"] == "正向" else -m["amount_cents"]
        for m in view["members"]
        if m["account_id"] == 1 and m["result"] == "已归并"
    )
    assert _balances(db)[1] - before[1] == expected_delta

    # 本趟报告：清单成员 9、成功+隔离=9、清单外 2，且互证仍一致
    report = client.post(f"/api/verify-reports?settle_run_id={run_id}").json()
    assert report["consistent"] == 1
    assert report["manifest_consistent"] == 1
    assert report["manifest_count"] == done["manifest_count"] == 9
    assert report["outside_arrival_count"] == 2
    assert report["detail"]["清单成员数"] == (
        report["detail"]["成功数"] + report["detail"]["隔离数"]
    )

    # 后续趟消费掉迟到的流水
    nxt = client.post("/api/settle-runs").json()
    assert nxt["status"] == "完成"
    assert nxt["manifest_count"] == 2
    assert nxt["outside_arrival_count"] == 0
    assert _entry_status(db, "FWD-LATE-001") == ("已归并", nxt["id"])
    assert _entry_status(db, "REV-LATE-001") == ("已归并", nxt["id"])
    assert _balances(db)[1] - before[1] == expected_delta
    assert client.post("/api/verify-reports").json()["consistent"] == 1


def test_integrity_failure_is_fail_closed(client, db):
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    run_id = frozen["id"]
    before = _balances(db)
    with sqlite3.connect(db) as conn:
        q_before = conn.execute("SELECT COUNT(*) FROM quarantine_items").fetchone()[0]
        victim = conn.execute(
            "SELECT entry_id, entry_key FROM settle_manifest_items WHERE settle_run_id = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        conn.execute(
            "UPDATE ledger_entries SET amount_cents = amount_cents + 1 WHERE id = ?",
            (victim[0],),
        )

    resp = client.post(f"/api/settle-runs/{run_id}/balance")
    assert resp.status_code == 409, resp.text
    assert "配平失败" in resp.text

    # 余额、流水、隔离项均未变；清单与运行标记失败
    assert _balances(db) == before
    assert _entry_status(db, victim[1]) == ("待归并", None)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quarantine_items").fetchone()[0] == q_before
        m = conn.execute(
            "SELECT status, failure_reason FROM settle_manifests WHERE settle_run_id = ?", (run_id,)
        ).fetchone()
        assert m[0] == "失败" and m[1]
        r = conn.execute(
            "SELECT status, failure_reason FROM settle_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert r[0] == "失败" and r[1]
    # 未完成运行不能出互证
    assert client.post(f"/api/verify-reports?settle_run_id={run_id}").status_code == 409

    # 恢复金额后不可重试已失败清单（状态非已固化）
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE ledger_entries SET amount_cents = amount_cents - 1 WHERE id = ?",
            (victim[0],),
        )
    assert client.post(f"/api/settle-runs/{run_id}/balance").status_code == 409
    # 后续趟恢复正常
    assert client.post("/api/settle-runs").json()["status"] == "完成"


def test_frozen_manifest_blocks_second_run(client, db):
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    resp = client.post("/api/settle-runs")
    assert resp.status_code == 409
    assert str(frozen["id"]) in resp.text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settle_manifests").fetchone()[0] == 1
    assert client.post(f"/api/settle-runs/{frozen['id']}/balance").status_code == 200
    assert client.post("/api/settle-runs").status_code == 200


def test_proof_pins_candidate_set_and_late_arrivals_are_drift(client, db):
    """回归：跑中候选漂移——迟到流水只能成为漂移键，不得进本趟正式配平。"""
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    run_id = frozen["id"]
    with sqlite3.connect(db) as conn:
        frozen_keys = {
            r[0]
            for r in conn.execute(
                "SELECT entry_key FROM settle_manifest_items WHERE settle_run_id = ?",
                (run_id,),
            )
        }
    assert len(frozen_keys) == 9

    # 固化之后、配平之前装载线程写入两条清单外流水
    _insert_pending(
        db, entry_id=901, entry_key="FWD-LATE-001", kind="正向",
        account_id=1, amount_cents=7000,
    )
    _insert_pending(
        db, entry_id=902, entry_key="REV-LATE-001", kind="冲正",
        account_id=1, amount_cents=7000, target="FWD-LATE-001",
    )

    done = client.post(f"/api/settle-runs/{run_id}/balance")
    assert done.status_code == 200, done.text
    done = done.json()
    assert done["manifest_count"] == 9
    assert done["outside_arrival_count"] == 2

    # 只读接口：按归并运行 id 取对照证明
    proof = client.get(f"/api/settle-runs/{run_id}/proof")
    assert proof.status_code == 200
    p = proof.json()
    assert p["status"] == "已配平"
    assert p["containment_ok"] == 1
    assert set(p["frozen_keys"]) == frozen_keys
    assert set(p["drift_keys"]) == {"FWD-LATE-001", "REV-LATE-001"}
    assert set(p["settled_keys"]).isdisjoint(p["drift_keys"])
    assert set(p["quarantine_keys"]).isdisjoint(p["drift_keys"])
    # 实际配平键 + 隔离键 ⊆ 固化键（清单外键不得进本趟正式配平）
    assert set(p["settled_keys"]) | set(p["quarantine_keys"]) <= frozen_keys
    assert p["frozen_count"] == 9
    assert p["settled_count"] + p["quarantine_count"] == 9
    assert p["drift_count"] == 2

    # 清单外键仍是待归并，余额未被它们触及
    assert _entry_status(db, "FWD-LATE-001") == ("待归并", None)
    assert _entry_status(db, "REV-LATE-001") == ("待归并", None)

    # 证明实体在库且不可变（只改报告计数无法伪造证据）
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE settle_manifest_proofs SET drift_count = 0 WHERE settle_run_id = ?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM settle_manifest_proofs WHERE settle_run_id = ?", (run_id,)
            )

    # 互证报告同源核对证明四组键
    report = client.post(f"/api/verify-reports?settle_run_id={run_id}").json()
    assert report["consistent"] == 1
    assert report["detail"]["清单对照证明一致"] is True
    assert report["detail"]["对照证明"]["漂移键"] == ["FWD-LATE-001", "REV-LATE-001"]

    # 清单视图同源携带证明，供归并台展示
    view = client.get(f"/api/settle-runs/{run_id}/manifest").json()
    assert view["proof"]["settle_run_id"] == run_id
    assert view["proof"]["frozen_keys"] == p["frozen_keys"]


def test_balance_reads_only_frozen_snapshot_not_live_pending_set(client, db):
    """路径 A：配平查询只读清单快照；删空实时待归并集也不影响本趟按清单完成。"""
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    run_id = frozen["id"]
    with sqlite3.connect(db) as conn:
        member_ids = [
            r[0]
            for r in conn.execute(
                "SELECT entry_id FROM settle_manifest_items WHERE settle_run_id = ?",
                (run_id,),
            )
        ]
        # 极端漂移：实时待归并集合被整体改走（状态不再是待归并），完整性检查必须拦下
        conn.executemany(
            "UPDATE ledger_entries SET status = '已归并' WHERE id = ?",
            [(i,) for i in member_ids],
        )
    resp = client.post(f"/api/settle-runs/{run_id}/balance")
    assert resp.status_code == 409
    # 失败闭：余额未动，出失败证明（实际配平键为空）
    assert _balances(db)[1] == 0
    p = client.get(f"/api/settle-runs/{run_id}/proof").json()
    assert p["status"] == "失败"
    assert p["settled_keys"] == []
    assert p["quarantine_keys"] == []
    assert p["frozen_count"] == 9


def test_containment_violation_rolls_back_whole_run(client, db, monkeypatch):
    """路径 B：若实际触及键跳出固化清单，提交前收口失败，整趟回滚本趟余额/状态。"""
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    run_id = frozen["id"]
    # 清单外迟到键（正常路径下只会被登记为漂移，保持待归并）
    _insert_pending(
        db, entry_id=951, entry_key="FWD-OUTSIDE-951", kind="正向",
        account_id=1, amount_cents=9999,
    )
    captured = {}
    real_check = fail_closed_settle._containment_check

    def sabotaged_check(conn, rid, member_ids, member_keys):
        # 模拟快照读取被绕过：配平逻辑把一条清单外流水也挂进本趟正式配平
        conn.execute(
            "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? "
            "WHERE entry_key = 'FWD-OUTSIDE-951'",
            (rid,),
        )
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + 9999, last_settle_run_id = ? "
            "WHERE id = 1",
            (rid,),
        )
        captured["ran"] = True
        real_check(conn, rid, member_ids, member_keys)

    monkeypatch.setattr(fail_closed_settle, "_containment_check", sabotaged_check)
    resp = client.post(f"/api/settle-runs/{run_id}/balance")
    assert captured.get("ran") is True
    assert resp.status_code == 409
    assert "收口失败" in resp.text

    # 整趟回滚：余额、流水状态、隔离项、清单结果全部回到配平前
    with connect() as conn:
        assert conn.execute("SELECT COALESCE(SUM(balance_cents),0) FROM accounts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE settle_run_id = ?", (run_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_items WHERE settle_run_id = ?", (run_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM settle_manifest_items WHERE settle_run_id = ? AND result IS NOT NULL",
            (run_id,),
        ).fetchone()[0] == 0
        head = conn.execute(
            "SELECT status FROM settle_manifests WHERE settle_run_id = ?", (run_id,)
        ).fetchone()
        assert head["status"] == "失败"

    # 失败证明留痕：实际配平键为空（越界写入已随回滚撤销）
    p = client.get(f"/api/settle-runs/{run_id}/proof").json()
    assert p["status"] == "失败"
    assert p["settled_keys"] == []
    assert p["containment_ok"] == 1
    # 仅还原收口函数；不能用 monkeypatch.undo()，它会连 DATA_DIR 一起还原导致串库
    monkeypatch.setattr(fail_closed_settle, "_containment_check", real_check)
    # 后续趟不受影响：新运行重新固化并配平成功
    nxt = client.post("/api/settle-runs").json()
    assert nxt["status"] == "完成"
    np = client.get(f"/api/settle-runs/{nxt['id']}/proof").json()
    assert np["status"] == "已配平" and np["containment_ok"] == 1


def test_proof_not_found_404_and_readonly(client):
    """证明接口只读：不存在的运行/尚未出证明的运行给 404，不提供写入口。"""
    missing = client.get("/api/settle-runs/9999/proof")
    assert missing.status_code == 404
    frozen = client.post("/api/settle-runs?freeze_only=true").json()
    not_yet = client.get(f"/api/settle-runs/{frozen['id']}/proof")
    assert not_yet.status_code == 404
    # 路由表里没有任何 proof 写接口
    paths = {r.path for r in client.app.routes if "proof" in getattr(r, "path", "")}
    assert paths == {"/api/settle-runs/{run_id}/proof"}
    assert all("GET" in r.methods for r in client.app.routes if getattr(r, "path", "") in paths)
