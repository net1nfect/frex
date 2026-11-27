---
name: authenticated-app-recon
description: Authenticated surface recon — establish a session, then crawl and fuzz behind login.
steps:
  - tool: session_authenticate
    params: {url: "{url}", method: "form", credentials: "{credentials}"}
  - tool: crawl_site
    params: {url: "{url}", session_token: "{session_token}"}
  - tool: content_discovery
    params: {url: "{url}", session_token: "{session_token}"}
---
Recon the authenticated surface of a web app. Establishes a session via
session_authenticate (the operator supplies credentials out of band),
then crawls and fuzzes with the session token attached. The login URL
is scope-checked before any credential is sent.
