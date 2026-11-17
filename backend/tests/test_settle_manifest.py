from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


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
