# vmware-001 — real Transform assessment

Derived from an actual AWS Transform console export
(`Initial_Migration_Assessment_20260901_191601.zip` — PPTX + XLSX + PDF, no JSON), run through
`tools/transform_ingest.py`.

## What the assessment says

- **2 servers**, both Ubuntu 16.04.7 LTS (Linux, "prod"), right-sized by Transform to **`c7a.medium`**
  (1 vCPU / 2 GiB, x86/AMD). Peak CPU ~48%, avg ~16%.
- **2 EBS GP3 volumes**, ~30 GiB provisioned / ~2.7 used each (9% utilisation), boot-only.
- Region **us-east-1**. Recommended pricing: **3-Year Reserved Instances (No Upfront)**.
- Cost basis (compute + network + storage, excl. AWS Business Support): on-demand **$84.84/mo**,
  1yr RI **$59.48/mo**, 3yr RI **$42.90/mo**. All-in with support ≈ $72/mo (3yr RI).
- Assessment issue: sustainability analysis excluded (missing on-prem host CPU data); storage
  sizing used default IOPS/throughput (missing peak data).

## Contract notes

- `budget.ceiling_monthly_usd = 43`, `pricing_model = "3yr_ri"` — the like-for-like number
  `monthly_run_rate` computes when `finops_resources` carry Transform's per-server `monthly_usd`.
- Only three, mild constraints: this is a low-risk straight rehost. No ARM restriction, no
  modernization ask, no managed-service dependencies — so the FBCTF golden traps do not fire here,
  by design.
- **Not captured as a constraint:** the target OS (Ubuntu 16.04) is end-of-life. There is no
  `os_eol` predicate in the vocabulary yet — it lives in the narrative only. Adding one is a design
  decision (what action does it gate, does it escalate?).
- Transform server ids (`server-f5a2cb54b18d7337`) are not MGN source-server ids or EC2 instance
  ids; the mapping comes from MGN discovery, later.
