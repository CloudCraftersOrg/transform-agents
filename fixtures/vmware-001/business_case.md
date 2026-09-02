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
2 servers and 2 storage volumes
 for AWS migration. The analysis reveals an estimated
annual savings of $64,525
 (
98.7%
) by migrating to AWS, with total recommended AWS cost of 
$863
 annually
compared to 
$65,387
 for on-premises infrastructure.
Assessment Scope
Compute
EC2:
 2 servers, 2 vCPUs, 6.0 GB memory
Storage
Block Storage (EBS):
 2 volumes, 60.00 GiB provisioned, 5.40 GiB used
On-Premises
On-Premises:
 2 Virtual Servers, 2 Server Volumes, 60.00 GiB storage
Please refer to the Assessment Issues sheet in the analysis workbook for additional details on issues found with
inventory.
Strategic Benefits
Strategic Benefit
Business Impact
Cost Optimization
Estimated annual savings of $64,525 (98.7%) with 3-Year Reserved Instances vs on-
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
Insights
 
AWS Transform
September 01, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
1
 of 
5
Financial Summary
Cost Component
AWS - On Demand
AWS - 1 Year No Upfront
AWS - 3 Year No Upfront
Compute
$899
$595
$396
Storage
$60
$60
$60
Network
$59
$59
$59
Business Support
$688
$483
$348
Annual Total
$1,706
$1,196
$863
Savings vs On-Demand
-
$510 (29.9%)
$844 (49.4%)
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
2 Virtual Servers, 2 Server Volumes
Cost Breakdown
Cost Category
Annual Cost
Compute
$29,892
Storage
$34,838
Network
$657
Total
$65,387
Assumptions
TCO Duration: 5 years
 
AWS Transform
September 01, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
2
 of 
5
AWS Region: us-east-1
Discounts applied: Hardware 40%, Software 25%, Storage 50%
Compute costs include: server and hardware costs (based on industry benchmarks for CPU and
Memory), software licensing costs (OS), facility costs
Storage costs include: server volumes
Network costs include: software, hardware, and bandwidth
Compute Assessment
Cost Summary
Cost Category
On Demand
1 Year No Upfront
3 Year No Upfront
Compute
$899
$595
$396
Network
$59
$59
$59
Total
$959
$654
$455
EC2 Migration
Scope
Metric
Value
Servers Analyzed
2 servers
Capacity Analyzed
2 vCPUs, 6.0 GB memory
Linux Servers
2
Cost Breakdown
Cost Category
On-Demand
1-Year Reserved Instances
3-Year Reserved Instances
Compute
$899
$595
$396
Network
$59
$59
$59
Total
$959
$654
$455
Key Findings
1
. 
Analyzed 2 active servers for EC2 migration
2
. 
Successfully mapped 2 servers to EC2 instances
3
. 
Recommended pricing: 3-Year Reserved Instances
4
. 
Right-sizing applied: actual peak utilization from inventory data (avg 48% CPU, 16% memory)
Recommendations
 
AWS Transform
September 01, 2026
 
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
(avg 48% CPU, 16% memory) to optimize costs.
Assumptions
Region: us-east-1
Tenancy: Shared
Pricing model: Reserved Instances (No Upfront)
Architecture: x86 (Intel and AMD)
Right-sizing: actual peak utilization from inventory data (avg 48% CPU, 16% memory)
Network cost: 10.0% of 1-Year Reserved Instances base instance cost
Storage Assessment
Scope
Metric
Value
EBS Storage
2 volumes
EBS Capacity
60.00 GiB provisioned, 5.40 GiB used
Cost Breakdown
Storage Service
Annual Cost
EBS
$60
Total
$60
Key Findings
1
. 
Only cost-optimized strategy is included in the assessment because 1-to-many and cost-optimized
strategies are identical. This may be due to missing peak IOPS and throughput data in the input
inventory which results in using default values from configuration
2
. 
All 2 storage volumes successfully mapped to EBS GP3 for optimal cost-performance balance
3
. 
Very low storage utilization detected - only 5.4 GiB used out of 60 GiB provisioned (9% utilization)
4
. 
Small-scale deployment with minimal storage footprint - annual cost of $59.52 for 2 virtual volumes
5
. 
Storage workload characteristics (using default 3000 IOPS and 125 MiB/s throughput) are well-
suited for GP3 standard performance tier
Recommendations
1
. 
Collect actual IOPS and throughput performance metrics
: The assessment used default
performance values (3000 IOPS, 125 MiB/s throughput) due to missing peak performance data in
the inventory. Collecting actual metrics will enable more accurate volume type selection and cost
optimization opportunities.
 
AWS Transform
September 01, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
4
 of 
5
2
. 
Right-size storage capacity
: Current storage utilization is only 9% (5.4 GiB used of 60 GiB
provisioned). Consider reducing provisioned capacity to match actual usage plus growth buffer to
optimize costs.
3
. 
Monitor workload performance requirements
: Establish baseline performance monitoring for
IOPS and throughput to ensure GP3 volumes continue to meet application needs as workload
scales.
Assumptions
Data volumes are mapped to Amazon EBS (GP3/ST1/SC1/IO2 BX) based on capacity and
performance requirements
Boot volumes are mapped to Amazon EBS GP3
30GB of boot disks considered for all in scope servers for directional costing
Boot disk size is removed from the actual used before evaluating data disk mapping
If used size is <30GiB, only boot disk is mapped, unmapped disks are ones with used capacity as 0
Adjusted capacity is 10% buffer on actual used (data disks)
Region: us-east-1
Disclaimer
This report provides an estimate of fees in USD, and savings based on certain information you provide. Fee estimates do
not include any taxes that might apply. Your actual fees and savings depend on a variety of factors, including your actual
usage of AWS services, which may vary from the estimates provided in this report. Additional configurations are available
on request by engaging a specialist.
 
AWS Transform
September 01, 2026
 
Copyright © 2026 Amazon Web Services, Inc. and/or its affiliates. All rights reserved.
Page 
5
 of 
5

## Structured facts (parsed from the analysis workbook)

Region: us-east-1
Recommended pricing model: 3yr_ri

### Servers (2)
- server-f5a2cb54b18d7337 (ip-10-20-10-205.ec2.internal): Ubuntu 16.04.7 LTS, RAM 2.0 GB, peak CPU 48.4%, avg CPU 1.2%, peak RAM 17.4%. Transform right-sizes to c7a.medium (1.0 vCPU, 2.0 GiB). Annualized total (compute+network): on_demand=$479.30 / 1yr_ri=$327.14 / 3yr_ri=$227.63.
- server-4dfd78a633075133 (ip-10-20-10-138.ec2.internal): Ubuntu 16.04.7 LTS, RAM 4.0 GB, peak CPU 49.1%, avg CPU 1.3%, peak RAM 16.1%. Transform right-sizes to c7a.medium (1.0 vCPU, 2.0 GiB). Annualized total (compute+network): on_demand=$479.30 / 1yr_ri=$327.14 / 3yr_ri=$227.63.

### Storage volumes (2)
- ip-10-20-10-205.ec2.internal-storage: EBS GP3, 30.0 GiB provisioned, 2.7 GiB used, boot 30.0 GiB. Annual cost $29.76.
- ip-10-20-10-138.ec2.internal-storage: EBS GP3, 30.0 GiB provisioned, 2.7 GiB used, boot 30.0 GiB. Annual cost $29.76.

### Assessment issues
- [WARNING] On prem host data is required for sustainability analysis -> Sustainability analysis is excluded from the assessment. Provide on-premises host data with valid CPU socket and core counts in the input data to enable sustainability analysis.

### Derived cost basis (compute + network + storage; excludes AWS Business Support)
- on_demand: $1,018.00/yr  ($84.83/month)
- 1yr_ri: $714.00/yr  ($59.50/month)
- 3yr_ri: $515.00/yr  ($42.92/month)
