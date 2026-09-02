# Discovery Tool Export

## Structured facts (parsed from the discovery export)

### Servers (12)
- mq-01 (10.70.1.32): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 1.9/29.9 GB. CPU p95 77.5% peak 100.0%, mem p95 17.6% peak 17.8%. env=n/a. processes: (no notable application processes captured)
- EC2AMAZ-K9LQ97Q (10.70.1.51): Microsoft Windows Server 2022 Datacenter 10.0.20348, 2.0 vCPU, 1.96 GB RAM, disk 39.9/80.0 GB. CPU p95 60.0% peak 100.0%, mem p95 88.2% peak 91.9%. env=n/a. processes: C:\Program Files\Microsoft SQL Server Reporting Services\SSRS\RSHostingService\RSHostingService.exe; C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Binn\fdhost.exe; C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Binn\fdlauncher.exe; C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Binn\launchpad.exe; C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Binn\sqlceip.exe; C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Binn\sqlservr.exe
- EC2AMAZ-HVB61P6 (10.40.0.125): Microsoft Windows Server 2022 Datacenter 10.0.20348, 2.0 vCPU, 3.94 GB RAM, disk 20.3/60.0 GB. CPU p95 100.0% peak 100.0%, mem p95 33.7% peak 33.7%. env=n/a. processes: (no notable application processes captured)
- ip-10-40-0-108 (10.40.0.108): Ubuntu 22.04.5 LTS (Jammy Jellyfish), 2.0 vCPU, 1.87 GB RAM, disk 2.7/38.6 GB. CPU p95 81.2% peak 81.2%, mem p95 29.5% peak 29.5%. env=n/a. processes: /usr/bin/python3 /usr/bin/networkd-dispatcher --run-startup-triggers; /usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal; /usr/sbin/apache2 -k start
- cache-01 (10.70.1.31): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 2.0/29.9 GB. CPU p95 98.6% peak 100.0%, mem p95 17.3% peak 17.4%. env=n/a. processes: /usr/bin/redis6-server 127.0.0.1:6379
- nfs-01 (10.70.1.33): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 1.9/29.9 GB. CPU p95 95.8% peak 100.0%, mem p95 18.2% peak 18.2%. env=n/a. processes: /usr/sbin/nfsdcld; /usr/sbin/rpc.idmapd
- ci-01 (10.70.1.41): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 2.4/29.9 GB. CPU p95 92.1% peak 97.1%, mem p95 39.7% peak 39.8%. env=n/a. processes: java -jar /opt/jenkins.war --httpPort=8080
- finance-batch-01 (10.70.1.22): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 2.0/29.9 GB. CPU p95 96.8% peak 100.0%, mem p95 17.6% peak 17.8%. env=n/a. processes: (no notable application processes captured)
- ip-10-40-10-96.ec2.internal (10.40.10.96): Amazon Linux 2023, 2.0 vCPU, 3.75 GB RAM, disk 4.3/79.9 GB. CPU p95 91.4% peak 91.4%, mem p95 34.9% peak 34.9%. env=n/a. processes: /bin/bash /opt/mssql/bin/launch_sqlservr.sh /opt/mssql/bin/sqlservr; /opt/mssql/bin/sqlservr; /usr/bin/containerd; /usr/bin/containerd-shim-runc-v2 -namespace moby -id 6144e78b8cd58120987281335cef6b086e8edbdd4bb887c5c6c085df999a056f -address /run/containerd/containerd.sock; /usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock --default-ulimit nofile=32768:65536
- ip-10-50-0-232.ec2.internal (10.50.0.232): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 2.2/29.9 GB. CPU p95 98.8% peak 100.0%, mem p95 40.4% peak 40.5%. env=n/a. processes: /usr/lib/jvm/java-1.8.0-amazon-corretto/bin/java -jar /opt/catalog/catalog.jar
- catalog-svc-01 (10.70.1.21): Amazon Linux 2023, 2.0 vCPU, 1.86 GB RAM, disk 2.2/29.9 GB. CPU p95 92.4% peak 94.1%, mem p95 18.7% peak 18.7%. env=n/a. processes: java Svc
- ip-10-50-10-208.ec2.internal (10.50.10.208): Amazon Linux 2023, 2.0 vCPU, 3.75 GB RAM, disk 6.8/59.9 GB. CPU p95 100.0% peak 100.0%, mem p95 66.7% peak 66.7%. env=n/a. processes: /bin/bash /opt/oracle/container-entrypoint.sh; /opt/oracle/product/21c/dbhomeXE/bin/tnslsnr LISTENER -inherit; /usr/bin/containerd; /usr/bin/containerd-shim-runc-v2 -namespace moby -id d638e61ecce8f85c0aa698b39cc0a7b751bb28bf0e9ebb57806cce761c7ea79f -address /run/containerd/containerd.sock; /usr/bin/coreutils --coreutils-prog-shebang=tail /usr/bin/tail -f /opt/oracle/diag/rdbms/xe/XE/trace/alert_XE.log; /usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock --default-ulimit nofile=32768:65536; oracleXE (LOCAL=NO)

### Databases (2)
- EC2AMAZ-K9LQ97Q: SQL Server 2022.160.4265.3 Express Edition (64-bit), instance MSSQLSERVER
- ip-10-50-10-208.ec2.internal: Oracle 21.3.0.0.0 XE, instance XE

### Dependencies (2 observed connections)
- ip-10-50-0-232.ec2.internal --[TCP 1521, java, 2993 conns]--> ip-10-50-10-208.ec2.internal
- ip-10-40-0-108 --[TCP 1433, ?, 48 conns]--> ip-10-40-10-96.ec2.internal

### Notes (auto-derived)
- SQL Server on EC2AMAZ-K9LQ97Q is a license-limited edition (Express Edition (64-bit)) - capacity/feature ceilings apply on migration.
- Oracle on ip-10-50-10-208.ec2.internal is a license-limited edition (XE) - capacity/feature ceilings apply on migration.
- EC2AMAZ-K9LQ97Q is memory-constrained (mem p95 88%).
- EC2AMAZ-K9LQ97Q is a Windows workload (Microsoft Windows Server 2022 Datacenter 10.0.20348).
- EC2AMAZ-HVB61P6 hits 100% CPU (p95 100%) and no application process was captured - review sizing.
- EC2AMAZ-HVB61P6 is a Windows workload (Microsoft Windows Server 2022 Datacenter 10.0.20348).
- cache-01 hits 100% CPU (p95 99%) - review sizing.
- nfs-01 hits 100% CPU (p95 96%) - review sizing.
- finance-batch-01 hits 100% CPU (p95 97%) and no application process was captured - review sizing.
- ip-10-40-10-96.ec2.internal already runs containers on the source host.
- ip-10-50-0-232.ec2.internal hits 100% CPU (p95 99%) - review sizing.
- ip-10-50-10-208.ec2.internal hits 100% CPU (p95 100%) - review sizing.
- ip-10-50-10-208.ec2.internal already runs containers on the source host.
