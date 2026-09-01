from __future__ import annotations

# Approximate monthly on-demand prices in us-east-1. Only for the run-rate at cutover (FinOps PoC).
INSTANCE_USD_MONTH = {
    "t3.micro": 7.49,
    "t3.small": 14.98,
    "t3.medium": 29.95,
    "t3.large": 59.90,
    "t4g.small": 12.10,
    "t4g.medium": 24.19,
    "m5.large": 69.12,
}
EBS_GP3_USD_GB_MONTH = 0.08


def monthly_run_rate(resources: list[dict]) -> float:
    """resources: [{instance_type, ebs_gib, ...}]. Unknown type = 0 compute (never invented)."""
    total = 0.0
    for r in resources:
        total += INSTANCE_USD_MONTH.get(r.get("instance_type", ""), 0.0)
        total += EBS_GP3_USD_GB_MONTH * float(r.get("ebs_gib", 0) or 0)
    return round(total, 2)
