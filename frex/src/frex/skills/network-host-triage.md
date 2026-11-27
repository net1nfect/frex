---
name: network-host-triage
description: Triage a single host or CIDR — port scan, then nuclei network templates against what is open.
steps:
  - tool: port_scan
    params: {target: "{target}"}
    stop_condition: "prev.count == 0"
  - tool: nuclei_scan
    params: {target: "{target}", tags: "network,nosql,db", severity: "info,low,medium"}
---
Triage a network host: enumerate open ports first, then run nuclei
network-oriented templates against the host. Stops early when the port
scan finds nothing open.
