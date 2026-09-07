# wave-0 — real AWS Transform migration plan

The wave-planning export for **Wave 0**, 12 servers / 3 applications. Same estate as
`discovery-001` and `mixed-estate-001` (the `import_*` ids trace back to the same
`discovery_tool_export.zip`), but this is the **plan**, not the assessment: it carries the wave,
the move groups, the per-server instance type MGN will launch, and the citations behind each
placement.

`plan/` holds the export verbatim (timestamps stripped from the filenames):

| File | What it carries |
|---|---|
| `mgn_import.csv` | the MGN import: 12 servers, `mgn:wave:name = Wave 0`, planned instance type, platform, private IP |
| `enriched_inventory.csv` | observed cores, system memory, CPU/memory avg **and peak**, move group |
| `applications.csv` | 3 apps with `migration_strategy`, environment, confidence, priority rank |
| `databases.csv` | database components (MSSQLSERVER, SSRS, Oracle XE), all `rehost` |
| `apps_to_move_groups.csv` | app → move group (mg_000 Unclassified, mg_001 Infra) |
| `citations.csv` | per-entity provenance: source file, source row, move-group rule, wave rule |
| `audit.json` | plan config (seed 42, group by environment, max 150/wave), input hashes, validator results |

## Why this fixture exists

**The plan under-sizes 9 of its own 12 servers**, and its three validators still report `pass`:

| Server(s) | Planned | Observed | Shortfall |
|---|---|---|---|
| mq-01, cache-01, nfs-01, finance-batch-01, catalog-svc-01 | `t3a.nano` (0.5 GiB) | 1.86 GiB | RAM ~4x short |
| ci-01, ip-10-50-0-232 | `t3a.micro` (1 GiB) | 1.86 GiB | RAM short |
| EC2AMAZ-K9LQ97Q | `t2.small` (1 vCPU) | 2 cores | vCPU short |
| ip-10-50-10-208 | `m7a.medium` (1 vCPU) | 2 cores | vCPU short |

`c2` (`min_ram_gib: 2`) and `c3` (`min_vcpu: 2`) are derived straight from
`metadata:total_system_memory_mebibytes` and `metadata:total_cpu_cores`, so
`evaluate_policy(Action("recommend_instance", ...))` refuses the planned instance for those nine.
That is the point of the fixture: the deterministic layer catching the plan's own error rather than
a synthetic trap.

## Modernization

All three applications carry `migration_strategy = rehost`, and the plan's own dashboard validates
`migration_strategy_uniformity` ("All waves contain only rehost strategy servers"). There is no
`modernization_unsupported` constraint, so the container path stays open: the modernization scenario
produces a **verified, never-applied** IaC artifact against a plan that documented `rehost` — the
"what the plan did not choose" half of deliverable 1.

## Budget

$259/month is the on-demand run-rate of the twelve planned instance types
(5x t3a.nano + 2x t3a.micro + t2.small + 3x c5a.large + m7a.medium). It is deliberately the cost of
the plan **as written** — right-sizing to satisfy `c2`/`c3` will exceed it, and that variance is the
FinOps agent's finding.
