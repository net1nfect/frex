---
name: web-app-recon
description: Full web application recon from a root domain or URL — subdomain enum, liveness via http_probe, tech fingerprint, content discovery, JS-aware crawl, then a tag-narrowed nuclei scan.
steps:
  - tool: subdomain_enum
    params: {domain: "{domain}"}
    stop_condition: "prev.count == 0"
  - tool: http_probe
    params: {url: "{url}"}
  - tool: tech_fingerprint
    params: {url: "{url}"}
  - tool: content_discovery
    params: {url: "{url}"}
  - tool: crawl_site
    params: {url: "{url}"}
  - tool: nuclei_scan
    params: {target: "{url}", tags: "{suggested_nuclei_tags}", severity: "info,low,medium"}
---
Run a complete web application recon pass. Enumerate subdomains for the
root domain, probe the main URL, fingerprint the tech stack, discover
paths and JS endpoints, then run a nuclei scan narrowed by the detected
technologies. Every step goes through the permission gate and scope
guard individually.
