from fastapi import APIRouter

from api.deps import get_normalized
from suffai.api.envelope import ok
from suffai.ingest.integrity import referential_integrity_report

router = APIRouter(prefix="/v1/integrity", tags=["integrity"])


@router.get("/report")
def integrity_report():
    report = referential_integrity_report(get_normalized())
    return ok(report, coverage={"gates": report.get("gates")})
