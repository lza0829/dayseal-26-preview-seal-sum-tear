from fastapi import APIRouter, Body
from app.services import close_service as svc

router = APIRouter(prefix="/close", tags=["close"])


@router.get("/preview")
def preview(business_day: str | None = None, declared_cash: float | None = None):
    # 只读展示 + 签发可绑定的权威预览快照（preview_id）
    return svc.preview(business_day, declared_cash)


@router.post("/seal")
def seal(body: dict = Body(...)):
    return svc.seal(body)


@router.get("/integrity")
def integrity():
    # 用现行流水复核每个 sealed 批次：批头/长短款/preview 是否三表同源
    return svc.integrity_report()


@router.post("/integrity/repair")
def repair(body: dict | None = Body(default=None)):
    # 流水力修复：作废所有与现行流水不一致的 sealed 批次，并恢复写闸
    day = (body or {}).get("business_day")
    return svc.void_inconsistent_batches(day)


@router.post("/batches/{batch_id}/void")
def void_batch(batch_id: int, body: dict | None = Body(default=None)):
    reason = (body or {}).get("reason") or "批次与现行流水不一致，作废并恢复写闸"
    return svc.void_batch(batch_id, reason)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/variances")
def variances():
    return svc.list_variances()
