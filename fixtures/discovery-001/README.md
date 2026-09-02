# discovery-001 — real discovery-tool export

Built from `discovery_tool_export.zip` (the CSV bundle you upload to AWS Transform / MPA), run
through `tools/transform_ingest.py::read_discovery`. Note: per the plan the discovery CSV is
another team's deliverable and a *Transform assessment output* would look like `vmware-001`. This
fixture exists because a business case is "free-form" and the ingest handles this format too.

## The estate (12 servers)

| Server | OS | Role (from process/dependency data) |
|---|---|---|
| catalog-svc-01, ci-01, ip-10-50-0-232 | Amazon Linux 2023 | Java services (`java Svc`, Jenkins, `catalog.jar`) |
| cache-01 | Amazon Linux 2023 | Redis 6 |
| nfs-01 | Amazon Linux 2023 | NFS server |
| mq-01, finance-batch-01 | Amazon Linux 2023 | message queue / batch (no clear app process) |
| ip-10-40-0-108 | Ubuntu 22.04 | Apache httpd |
| ip-10-40-10-96 | Amazon Linux 2023 | SQL Server on Linux, in Docker |
| ip-10-50-10-208 | Amazon Linux 2023 | Oracle XE 21c, in Docker |
| EC2AMAZ-K9LQ97Q | Windows Server 2022 | SQL Server 2022 **Express** + SSRS (memory-constrained, p95 88%) |
| EC2AMAZ-HVB61P6 | Windows Server 2022 | **unknown** — 100% CPU, no app process captured |

Dependencies observed: `catalog.jar` → Oracle XE (`:1521`, 2993 conns); Apache → SQL Server
(`:1433`, 48 conns).

## Contract notes

- `c1 sizing_basis = peak_with_headroom` — avg CPU ~20% but p95 95-100%; averaging would undersize.
- `c2 min_ram_gib = 2` — Oracle XE and SQL Server Express both need >= 2 GiB; source hosts are 1.86.
- `c3 out_of_scope` on **EC2AMAZ-HVB61P6** — pinned at 100% CPU with no identifiable workload; any
  action on it is denied until it's identified (that's the deliverable-5 escalation for this case).
- `budget` is **directional** — a discovery export carries no Transform costing.

## Both approaches, on this workload

- **Lift-and-shift (executed):** every in-scope server via MGN.
- **Modernization (verified IaC artifact, not applied):** the stateless Linux services
  (`catalog.jar`, Jenkins, Redis, Apache) → ECS/Fargate. The databases (Oracle XE, SQL Server) are
  *not* containerized — they'd move to RDS or stay on EC2; the Windows boxes rehost. Run a wave with
  `WaveInputs(modernization_target="container")` to produce the artifact.
