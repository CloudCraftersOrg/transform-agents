# AWS Transform Migration Assessment

## Assessment report (narrative)

AWS Migration Business Case
Table of Contents
Executive Summary
Insights
Next Steps
On-Premises Assessment
Compute Assessment
Storage Assessment
Disclaimer
Executive Summary
This assessment analyzed 
12 servers and 12 storage volumes
 for AWS migration. The analysis reveals an estimated
annual savings of $24,884
 (
76.3%
) by migrating to AWS, with total recommended AWS cost of 
$7,741
 annually
compared to 
$32,626
 for on-premises infrastructure.
Assessment Scope
Compute
EC2:
 12 servers, 24 vCPUs, 28.3 GB memory
Storage
Block Storage (EBS):
 11 volumes, 498.02 GiB provisioned, 86.61 GiB used
File Storage (FSx):
 1 storage devices, 1.95 GiB
On-Premises
On-Premises:
 12 Virtual Servers, 0.00 GiB storage
Please refer to the Assessment Issues sheet in the analysis workbook for additional details on issues found with
inventory.
Strategic Benefits
Strategic Benefit
Business Impact
Cost Optimization
Estimated annual savings of $24,884 (76.3%) with 3-Year Reserved Instances vs on-
demand
Infrastructure
Modernization
Migration from aging bare metal to cloud-native, right-sized EC2 instances
Operational Efficiency
Elimination of hardware maintenance, power, cooling, and data center overhead costs
Enhanced Scalability
On-demand resource scaling to meet changing business requirements
Improved Reliability
AWS 99.99% SLA with built-in redundancy and disaster recovery capabilities
 
AWS Transform
September 02, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
1
 of 
5
Insights
Financial Summary
Cost Component
AWS - On Demand
AWS - 1 Year No Upfront
AWS - 3 Year No Upfront
Compute
$9,654
$6,763
$5,076
Storage
$1,447
$1,447
$1,447
Network
$515
$515
$515
Business Support
$1,162
$872
$704
Annual Total
$12,777
$9,597
$7,741
Savings vs On-Demand
-
$3,180 (24.9%)
$5,035 (39.4%)
Next Steps
1
. 
Engage Specialists:
 
Engage a specialist
 if you want further support and guidance on your
migration journey.
2
. 
Perform wave planning and migration:
 Utilize the AWS Transform VMware job capabilities to
perform comprehensive application assessment through dependency mapping, create migration
waves, and execute the VMware-to-EC2 migrations
3
. 
Engage AWS Migration Acceleration Program:
 Apply for the 
AWS MAP program
 to potentially
offset migration costs and accelerate your cloud journey through expertise from AWS Professional
Services, a global partner ecosystem, and AWS investment.
On-Premises Assessment
Scope
Metric
Value
Total Servers
12 Virtual Servers
Cost Breakdown
Cost Category
Annual Cost
Compute
$31,968
Network
$657
Total
$32,626
Assumptions
TCO Duration: 5 years
 
AWS Transform
September 02, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
2
 of 
5
AWS Region: us-east-1
Discounts applied: Hardware 40%, Software 25%, Storage 50%
Compute costs include: server and hardware costs (based on industry benchmarks for CPU and
Memory), software licensing costs (OS), facility costs
Network costs include: software, hardware, and bandwidth
Compute Assessment
Cost Summary
Cost Category
On Demand
1 Year No Upfront
3 Year No Upfront
Compute
$9,654
$6,763
$5,076
Network
$515
$515
$515
Total
$10,169
$7,278
$5,591
EC2 Migration
Scope
Metric
Value
Servers Analyzed
12 servers
Capacity Analyzed
24 vCPUs, 28.3 GB memory
Windows Servers
2
Linux Servers
10
Cost Breakdown
Cost Category
On-Demand
1-Year Reserved Instances
3-Year Reserved Instances
Compute
$9,654
$6,763
$5,076
Network
$515
$515
$515
Total
$10,169
$7,278
$5,591
Key Findings
1
. 
Analyzed 12 active servers for EC2 migration
2
. 
Successfully mapped 12 servers to EC2 instances
3
. 
Recommended pricing: 3-Year Reserved Instances
4
. 
Right-sizing applied: actual peak utilization from inventory data (avg 90% CPU, 36% memory)
Recommendations
 
AWS Transform
September 02, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
3
 of 
5
1
. 
Implement Reserved Instance Strategy
: Purchase 3-Year Reserved Instances for predictable
workloads to achieve savings over on-demand pricing.
2
. 
Right-Sizing Applied
: Instance sizes adjusted based on actual peak utilization from inventory data
(avg 90% CPU, 36% memory) to optimize costs.
Assumptions
Region: us-east-1
Tenancy: Shared
Pricing model: Reserved Instances (No Upfront)
Architecture: x86 (Intel and AMD)
Right-sizing: actual peak utilization from inventory data (avg 90% CPU, 36% memory)
Network cost: 10.0% of 1-Year Reserved Instances base instance cost
Storage Assessment
Scope
Metric
Value
EBS Storage
11 volumes
EBS Capacity
498.02 GiB provisioned, 86.61 GiB used
FSx Storage
1 storage devices
FSx Capacity
1.95 GiB
Cost Breakdown
Storage Service
Annual Cost
EBS
$337
FSx
$1,110
Total
$1,447
Key Findings
1
. 
All storage volumes (11 virtual) mapped to EBS GP3, the most cost-effective general-purpose SSD
option
2
. 
Only cost-optimized strategy is included in the assessment because 1-to-many and cost-optimized
strategies are identical. This may be due to missing peak IOPS and throughput data in the input
inventory which results in using default values from configuration
3
. 
Very low storage utilization detected: only 86.61 GiB used capacity across 498.02 GiB provisioned
(17.4% utilization)
4
. 
Boot volumes dominate the storage footprint: 330 GB boot disk space (94% of total) vs 20.89 GB
data disk space (6%)
5
. 
All volumes fit within GP3 baseline performance (3,000 IOPS, 125 MiB/s), resulting in no additional
 
AWS Transform
September 02, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
4
 of 
5
IOPS or throughput charges
Recommendations
1
. 
Right-size virtual machine storage volumes
: Storage volumes are significantly over-provisioned
with only 17.4% utilization (86.61 GiB used out of 498.02 GiB provisioned). Consider reducing
provisioned capacity to match actual usage patterns plus growth buffer.
2
. 
Improve performance monitoring data collection
: Peak IOPS and throughput data appear to be
missing or using default values, resulting in identical optimization strategies. Implement
comprehensive storage performance monitoring to enable accurate workload characterization and
optimization opportunities.
3
. 
Validate boot volume sizing requirements
: Boot volumes are configured at 30 GB each (330 GB
total for 11 volumes), which dominates the storage footprint. Review whether all virtual machines
require 30 GB boot volumes or if smaller sizes would suffice.
4
. 
Review throughput tier selection for cost optimization
: The provisioned throughput tier (128
MiB/s) accounts for 99.7% of the total cost ($92.16/month). With actual peak throughput of 4.55
MiB/s and required throughput of 2.27 MiB/s, the current tier may be significantly over-provisioned.
Consider validating actual throughput requirements and adjusting the tier selection to reduce costs.
5
. 
Consider Multi-AZ deployment for high availability requirements
: Current configuration uses
Single-AZ deployment for cost optimization ($1,109.78 annually). If business continuity and high
availability are critical requirements, Multi-AZ deployment ($1,850.92 annually) provides automatic
failover and data replication across availability zones for 67% additional cost.
Assumptions
Data volumes are mapped to Amazon EBS (GP3/ST1/SC1/IO2 BX) based on capacity and
performance requirements
Boot volumes are mapped to Amazon EBS GP3
30GB of boot disks considered for all in scope servers for directional costing
Boot disk size is removed from the actual used before evaluating data disk mapping
If used size is <30GiB, only boot disk is mapped, unmapped disks are ones with used capacity as 0
Adjusted capacity is 10% buffer on actual used (data disks)
Region: us-east-1
Assessment performed for AWS region: us-east-1
Deployment option: single_az
Disclaimer
This report provides an estimate of fees in USD, and savings based on certain information you provide. Fee estimates do
not include any taxes that might apply. Your actual fees and savings depend on a variety of factors, including your actual
usage of AWS services, which may vary from the estimates provided in this report. Additional configurations are available
on request by engaging a specialist.
 
AWS Transform
September 02, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
5
 of 
5

## Structured facts (parsed from the analysis workbook)

Region: us-east-1
Recommended pricing model: 3yr_ri

### Servers (12)
- server-4c0392b726b157f0 (mq-01): Amazon Linux, RAM 1.86 GB, peak CPU 100.0%, avg CPU 17.5%, peak RAM 17.8%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-fce322bfbb4d4af4 (EC2AMAZ-K9LQ97Q): Microsoft Windows Server 2022 Datacenter, RAM 1.96 GB, peak CPU 100.0%, avg CPU 11.1%, peak RAM 91.9%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$1,518.98 / 1yr_ri=$1,278.08 / 3yr_ri=$1,135.46.
- server-b60b5e10f0dc4ce4 (EC2AMAZ-HVB61P6): Microsoft Windows Server 2022 Datacenter, RAM 3.94 GB, peak CPU 100.0%, avg CPU 100.0%, peak RAM 33.7%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$1,518.98 / 1yr_ri=$1,278.08 / 3yr_ri=$1,135.46.
- server-f05ca18514347f16 (ip-10-40-0-108): Ubuntu, RAM 1.87 GB, peak CPU 81.2%, avg CPU 81.2%, peak RAM 29.5%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-991981a078a6cd84 (cache-01): Amazon Linux, RAM 1.86 GB, peak CPU 100.0%, avg CPU 20.9%, peak RAM 17.4%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-2657d66a1da1e3a0 (nfs-01): Amazon Linux, RAM 1.86 GB, peak CPU 100.0%, avg CPU 20.9%, peak RAM 18.2%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-cfac270c76cdd0b8 (ci-01): Amazon Linux, RAM 1.86 GB, peak CPU 97.1%, avg CPU 20.8%, peak RAM 39.8%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-9df3720e47df8c56 (finance-batch-01): Amazon Linux, RAM 1.86 GB, peak CPU 100.0%, avg CPU 21.8%, peak RAM 17.8%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-b99b78747d797852 (ip-10-40-10-96.ec2.internal): Amazon Linux, RAM 3.75 GB, peak CPU 91.4%, avg CPU 91.4%, peak RAM 34.9%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-75c4176bf0a0fbc3 (ip-10-50-0-232.ec2.internal): Amazon Linux, RAM 1.86 GB, peak CPU 100.0%, avg CPU 20.3%, peak RAM 40.5%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-de72fabe0cb6bda5 (catalog-svc-01): Amazon Linux, RAM 1.86 GB, peak CPU 94.1%, avg CPU 19.1%, peak RAM 18.7%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.
- server-86eead48cde6fc7a (ip-10-50-10-208.ec2.internal): Amazon Linux, RAM 3.75 GB, peak CPU 100.0%, avg CPU 22.8%, peak RAM 66.7%. Transform right-sizes to c6a.large (2.0 vCPU, 4.0 GiB). Annualized total (compute+network): on_demand=$713.06 / 1yr_ri=$472.16 / 3yr_ri=$332.00.

### Storage volumes (12)
- import_0705e40e-dd07-5423-b3ca-e8cea03e8c1c-nvme0n1: EBS GP3, 29.93 GiB provisioned, 1.9 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_0b6b01c5-d120-5bce-8c8f-d8a57805bc35-0 c:: EBS GP3, 80.0 GiB provisioned, 39.9 GiB used, boot 30.0 GiB. Annual cost $39.25.
- import_3e51316c-c467-5519-b24b-939f6f02b8d7-0 c:: EBS GP3, 60.0 GiB provisioned, 20.3 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_44784c54-26eb-5f62-98a6-b1770a879c26-nvme0n1: EBS GP3, 38.58 GiB provisioned, 2.74 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_4d2ba2dd-1c65-54c9-8961-706f9bc2c716-nvme0n1: EBS GP3, 29.93 GiB provisioned, 1.96 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_99b69105-fed0-5c04-8b6a-54651a9c3e47-nvme0n1: EBS GP3, 29.93 GiB provisioned, 2.41 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_a30b55c1-0587-5029-839d-71f30905225f-nvme0n1: EBS GP3, 29.93 GiB provisioned, 1.96 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_a8961f84-8e64-5327-9ea6-a9cdcb181127-nvme0n1: EBS GP3, 79.93 GiB provisioned, 4.27 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_bf97cd90-d6c9-550e-9ca9-4bea1c402677-nvme0n1: EBS GP3, 29.93 GiB provisioned, 2.24 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_da2c6743-6a4f-54db-9c8d-5227933d64fd-nvme0n1: EBS GP3, 29.93 GiB provisioned, 2.18 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_e0b56d26-039f-508a-af2f-774aba6ec4f8-nvme0n1: EBS GP3, 59.93 GiB provisioned, 6.75 GiB used, boot 30.0 GiB. Annual cost $29.76.
- import_7a60d1e0-995a-53c0-84e3-58df40793c90-nvme0n1: FSx for NetApp ONTAP, 29.93 GiB provisioned, 1.95 GiB used.

### Assessment issues
- [WARNING] On prem host data is required for sustainability analysis -> Sustainability analysis is excluded from the assessment. Provide on-premises host data with valid CPU socket and core counts in the input data to enable sustainability analysis.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 3.8 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 3.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 3.8 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 1.9 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.
- [WARNING] vCPU specification missing or invalid. -> Using default 2 vCPUs (actual memory: 2.0 GB). Server is assessed using default specifications; cost estimates may be inaccurate.. Update inventory with CPU core count. Re-run assessment for accurate results.

### Derived cost basis (compute + network + storage; excludes AWS Business Support)
- on_demand: $11,616.00/yr  ($968.00/month)
- 1yr_ri: $8,725.00/yr  ($727.08/month)
- 3yr_ri: $7,038.00/yr  ($586.50/month)
