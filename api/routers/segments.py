from fastapi import APIRouter

from api.deps import get_rfm
from suffai.api.envelope import ok
from suffai.metrics.rfm import rfm_distribution, RFM_RULE_VERSION

router = APIRouter(prefix="/v1/segments", tags=["segments"])


@router.get("/summary")
def segments_summary():
    snaps = get_rfm()
    return ok(
        {
            "distribution": rfm_distribution(snaps),
            "customers_scored": len(snaps),
            "rule_version": RFM_RULE_VERSION,
            "definition": (
                "R/F/M from eligible delivered+paid orders; "
                "M is laundry spend, not garment purchase spend."
            ),
        },
        as_of=snaps[0]["as_of_date"] if snaps else None,
    )
