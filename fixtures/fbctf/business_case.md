# FBCTF — Migration Business Case (as delivered to the autonomy team)

## Workload

Facebook CTF, two EC2 instances behind a public ALB (no TLS) and an internal NLB:

- **Web tier** `i-0422e26203cb709b2` — t3.small, nginx, Ubuntu 16.04 (EOL). Proxies FastCGI
  on port 9000 to the app tier.
- **App tier** `i-0d6b944117ba6302b` — t3.medium, HHVM 3.21, Ubuntu 16.04 (EOL).

Managed services: RDS MySQL single-AZ with no backups, an ElastiCache node that is essentially
idle, a NAT gateway, Secrets Manager and CloudWatch. **Current run rate is about USD 190 / month.**
The budget for the migrated workload is the as-is run rate with a tolerance of 15%.

## Observed utilisation

Over the last 30 days the app tier averaged **1.2% CPU** and **17% memory**. The web tier is
similarly quiet. These averages are misleading: the workload is bursty during scored events.
**Do not recommend anything below 2 vCPU / 2 GiB, and size on peak with headroom rather than on
average utilisation.**

## Architecture constraints

- Graviton is attractive on price/performance for the web tier. However, **HHVM 3.21 has no
  ARM64 build**, so the application tier cannot move to arm64.
- **AWS Transform code modernization does not cover PHP/Hack**, so the HHVM application cannot be
  modernised through Transform. A rehost is the only supported path for that tier.

## Cost attribution note

EC2 and EBS resources are untagged, so current spend cannot be attributed per component without
additional work.
