---
name: finding-confirmation
description: Confirm a single nuclei finding — poll the OOB interaction when the template is blind, take a screenshot as visual evidence, then record the confirmed fact.
steps:
  - tool: interactsh_poll
    params: {correlation_id: "{correlation_id}"}
  - tool: screenshot
    params: {url: "{url}"}
  - tool: submit_facts
    params: {facts: "[{description: \"{description} (confirmed)\", kind: \"finding\", confidence: 0.9}]"}
---
Confirm one finding: poll the out-of-band interaction (blind payloads),
capture visual evidence, then submit the fact with a confidence bump.
