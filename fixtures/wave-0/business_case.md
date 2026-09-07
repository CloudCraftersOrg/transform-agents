# AWS Transform Migration Plan - Wave 0

## Structured facts (parsed from the wave-planning export)

### Applications (3)
- Contoso Scoreboard [app_contoso_scoreboard]: strategy=rehost, env=infra, 8 servers, move group Infra-MoveGroup, confidence 0.8799999999999999, priority rank 1
- Project Nami [app_project_nami]: strategy=rehost, env=unclassified, 2 servers, move group Unclassified-MoveGroup, confidence 1.0, priority rank 2
- Contoso Catalog [app_contoso_catalog]: strategy=rehost, env=unclassified, 2 servers, move group Unclassified-MoveGroup, confidence 1.0, priority rank 3

### Database components (3)
- MSSQLSERVER [import_0b6b01c5-d120-5bce-8c8f-d8a57805bc35:MSSQLSERVER]: strategy=rehost
- SSRS [import_0b6b01c5-d120-5bce-8c8f-d8a57805bc35:SQLServerReportingServices]: strategy=rehost
- XE [import_e0b56d26-039f-508a-af2f-774aba6ec4f8:XE]: strategy=rehost

### Servers in Wave 0 (12)
- EC2AMAZ-HVB61P6.WORKGROUP (10.40.0.125): Windows, planned instance c5a.large, observed 2 cores / 3.94 GiB. CPU avg 100.0% peak 100.0%, mem avg 33.7% peak 33.7%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- EC2AMAZ-K9LQ97Q.WORKGROUP (10.70.1.51): Windows, planned instance t2.small, observed 2 cores / 1.96 GiB. CPU avg 11.1% peak 100.0%, mem avg 80.5% peak 91.9%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- cache-01 (10.70.1.31): Linux, planned instance t3a.nano, observed 2 cores / 1.86 GiB. CPU avg 20.9% peak 100.0%, mem avg 17.1% peak 17.4%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- catalog-svc-01 (10.70.1.21): Linux, planned instance t3a.nano, observed 2 cores / 1.86 GiB. CPU avg 19.1% peak 94.1%, mem avg 18.1% peak 18.7%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- ci-01 (10.70.1.41): Linux, planned instance t3a.micro, observed 2 cores / 1.86 GiB. CPU avg 20.8% peak 97.1%, mem avg 39.0% peak 39.8%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- finance-batch-01 (10.70.1.22): Linux, planned instance t3a.nano, observed 2 cores / 1.86 GiB. CPU avg 21.8% peak 100.0%, mem avg 17.0% peak 17.8%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- ip-10-40-0-108.ec2.internal (10.40.0.108): Linux, planned instance c5a.large, observed 2 cores / 1.87 GiB. CPU avg 81.2% peak 81.2%, mem avg 29.5% peak 29.5%. app=Project Nami, move group=Unclassified-MoveGroup
- ip-10-40-10-96.ec2.internal (10.40.10.96): Linux, planned instance c5a.large, observed 2 cores / 3.75 GiB. CPU avg 91.4% peak 91.4%, mem avg 34.9% peak 34.9%. app=Project Nami, move group=Unclassified-MoveGroup
- ip-10-50-0-232.ec2.internal (10.50.0.232): Linux, planned instance t3a.micro, observed 2 cores / 1.86 GiB. CPU avg 20.3% peak 100.0%, mem avg 34.9% peak 40.5%. app=Contoso Catalog, move group=Unclassified-MoveGroup
- ip-10-50-10-208.ec2.internal (10.50.10.208): Linux, planned instance m7a.medium, observed 2 cores / 3.75 GiB. CPU avg 22.8% peak 100.0%, mem avg 66.3% peak 66.7%. app=Contoso Catalog, move group=Unclassified-MoveGroup
- mq-01 (10.70.1.32): Linux, planned instance t3a.nano, observed 2 cores / 1.86 GiB. CPU avg 17.5% peak 100.0%, mem avg 17.2% peak 17.8%. app=Contoso Scoreboard, move group=Infra-MoveGroup
- nfs-01 (10.70.1.33): Linux, planned instance t3a.nano, observed 2 cores / 1.86 GiB. CPU avg 20.9% peak 100.0%, mem avg 17.7% peak 18.2%. app=Contoso Scoreboard, move group=Infra-MoveGroup

### Plan sizing shortfalls (auto-derived, 9 of 12 servers)
- EC2AMAZ-K9LQ97Q.WORKGROUP: plan assigns t2.small but 1 vCPU < 2 cores observed (peak CPU 100.0%, peak mem 91.9%)
- cache-01: plan assigns t3a.nano but 0.5 GiB RAM < 1.86 GiB observed (peak CPU 100.0%, peak mem 17.4%)
- catalog-svc-01: plan assigns t3a.nano but 0.5 GiB RAM < 1.86 GiB observed (peak CPU 94.1%, peak mem 18.7%)
- ci-01: plan assigns t3a.micro but 1.0 GiB RAM < 1.86 GiB observed (peak CPU 97.1%, peak mem 39.8%)
- finance-batch-01: plan assigns t3a.nano but 0.5 GiB RAM < 1.86 GiB observed (peak CPU 100.0%, peak mem 17.8%)
- ip-10-50-0-232.ec2.internal: plan assigns t3a.micro but 1.0 GiB RAM < 1.86 GiB observed (peak CPU 100.0%, peak mem 40.5%)
- ip-10-50-10-208.ec2.internal: plan assigns m7a.medium but 1 vCPU < 2 cores observed (peak CPU 100.0%, peak mem 66.7%)
- mq-01: plan assigns t3a.nano but 0.5 GiB RAM < 1.86 GiB observed (peak CPU 100.0%, peak mem 17.8%)
- nfs-01: plan assigns t3a.nano but 0.5 GiB RAM < 1.86 GiB observed (peak CPU 100.0%, peak mem 18.2%)

### Derived cost basis (arithmetic, from the planned instance types)
- on-demand run-rate of the 12 planned instances: $258.56/month

### Plan validators
- wave_plan_validation: pass
- constraint_validation: pass
- golden_pack_validation: pass (3/3 packs)

### Plan configuration
- grouped by ['environment'], priority [{'environment': ['infra', 'unclassified']}], max 150 servers per wave, seed 42

### Provenance
- 12 entities cited from: discovery_tool_export.zip/full_exports_server_inventory.csv
