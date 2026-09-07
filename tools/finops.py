from __future__ import annotations

# Approximate monthly on-demand compute prices in us-east-1. Only a fallback: prefer a per-resource
# `monthly_usd` (from Transform's own cost output) over guessing from this table.
INSTANCE_USD_MONTH = {
    "t3.micro": 7.49,
    "t3.small": 14.98,
    "t3.medium": 29.95,
    "t3.large": 59.90,
    "t4g.small": 12.10,
    "t4g.medium": 24.19,
    "m5.large": 69.12,
    "c7a.medium": 37.46,
    "t3a.nano": 3.43,
    "t3a.micro": 6.86,
    "t3a.small": 13.72,
    "t2.small": 16.79,
    "c5a.large": 56.21,
    "m7a.medium": 42.27,
}
EBS_GP3_USD_GB_MONTH = 0.08

# Directional Reserved-Instance discount factors on the compute portion (from an observed Transform
# assessment: 3yr NU compute ~0.44 of on-demand, 1yr NU ~0.66). Real numbers come from Transform.
RI_FACTOR = {"on_demand": 1.0, "1yr_ri": 0.66, "3yr_ri": 0.44}


def monthly_run_rate(resources: list[dict], *, pricing_model: str = "on_demand") -> float:
    """resources: [{instance_type, ebs_gib, monthly_usd?, ...}]. If a resource carries `monthly_usd`
    (compute + network, from Transform) it's used as-is; otherwise the price table is used with the
    pricing-model discount. Unknown type = 0 compute (never invented). EBS is added on top."""
    total = 0.0
    for r in resources:
        if r.get("monthly_usd") is not None:
            total += float(r["monthly_usd"])
        else:
            base = INSTANCE_USD_MONTH.get(r.get("instance_type", ""), 0.0)
            total += base * RI_FACTOR.get(pricing_model, 1.0)
        total += EBS_GP3_USD_GB_MONTH * float(r.get("ebs_gib", 0) or 0)
    return round(total, 2)
