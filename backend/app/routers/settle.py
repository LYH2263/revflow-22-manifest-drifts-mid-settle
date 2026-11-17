from fastapi import APIRouter, Query

from app.services import settle as svc

router = APIRouter(tags=["归并"])


@router.get("/settle-runs")
def get_runs():
    return svc.list_settle_runs()


@router.get("/settle-runs/{run_id}")
def get_run(run_id: int):
    return svc.get_settle_run(run_id)


@router.get("/settle-runs/{run_id}/manifest")
def get_manifest(run_id: int):
    return svc.get_manifest_view(run_id)


@router.get("/settle-runs/{run_id}/proof")
def get_run_proof(run_id: int):
    """只读：按归并运行 id 查询清单对照证明（固化/实际配平/漂移/隔离四组键）。"""
    return svc.get_run_proof(run_id)


@router.post("/settle-runs")
def post_run(freeze_only: bool = Query(False, description="只固化本趟消费清单，不配平")):
    return svc.run_settle(freeze_only=freeze_only)


@router.post("/settle-runs/{run_id}/balance")
def post_balance(run_id: int):
    return svc.balance_frozen_run(run_id)
