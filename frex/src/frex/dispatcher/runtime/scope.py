"""Scope guard: validate tool targets against the operation's scope.

The scope lives on the project (domains, CIDRs, exclusions). Every
check returns a rejection message instead of raising — the agent loop
feeds rejections back to the model, so an out-of-scope call costs one
round trip, not a task failure.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(slots=True)
class Scope:
    domains: list[str] = field(default_factory=list)
    cidrs: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    exclude_cidrs: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_json(cls, raw: str | None) -> "Scope | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return cls(
            domains=[d.lower() for d in data.get("domains", [])],
            cidrs=data.get("cidrs", []),
            exclude_domains=[d.lower() for d in data.get("exclude_domains", [])],
            exclude_cidrs=data.get("exclude_cidrs", []),
            notes=data.get("notes", ""),
        )

    @classmethod
    def from_config(cls, config: Any | None) -> "Scope | None":
        if config is None:
            return None
        return cls(
            domains=[d.lower() for d in (config.domains or [])],
            cidrs=list(config.cidrs or []),
            exclude_domains=[d.lower() for d in (config.exclude_domains or [])],
            exclude_cidrs=list(config.exclude_cidrs or []),
            notes=config.notes or "",
        )

    def _networks(self, raw_list: list[str]):
        networks = []
        for cidr in raw_list:
            try:
                networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        return networks

    def _host_in_cidrs(self, host: str, raw_list: list[str]) -> bool:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self._networks(raw_list))

    @staticmethod
    def _subdomain_of(host: str, allowed: str) -> bool:
        # host is allowed iff it equals the entry or is a subdomain of
        # it. Deliberately no registrable-domain heuristic: allowing
        # target.example.com must not allow evil.example.com.
        return host == allowed or host.endswith("." + allowed)

    def check_host(self, host: str) -> str | None:
        """Return a rejection message when host is out of scope."""
        host = host.strip().lower()
        if not host:
            return None
        if self._host_in_cidrs(host, self.exclude_cidrs):
            return f"host {host} is explicitly excluded from scope"
        if self._host_in_cidrs(host, self.cidrs):
            return None
        if any(self._subdomain_of(host, d) for d in self.exclude_domains):
            return f"domain {host} is explicitly excluded from scope"
        if self.exclude_domains and not self.domains and not self.cidrs:
            # Exclusion-only scope: everything else is allowed.
            return None
        if any(self._subdomain_of(host, d) for d in self.domains):
            return None
        if self.domains or self.cidrs:
            return f"host {host} is out of scope (allowed: {', '.join(self.domains + self.cidrs)})"
        return None  # No scope defined: unrestricted.

    def check_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return self.check_host(host)

    def check_command(self, command: str) -> str | None:
        """Heuristic: extract host-like tokens from a shell command.

        Not a parser — a last-line guard. Command scoping can never be
        complete (base64, variables), which is why this is defence in
        depth behind operator-approved scope, not the only gate.
        """
        import re

        tokens = re.findall(
            r"\b(?:\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?|"
            r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})\b",
            command,
        )
        for token in tokens:
            # CIDR notation passes through check_host via its network
            # part: 10.0.0.0/24 is in scope iff 10.0.0.0 is.
            host = token.split("/")[0]
            if "/" in token:
                try:
                    import ipaddress

                    network = ipaddress.ip_network(token, strict=False)
                except ValueError:
                    return f"target {token} is not a valid address or network"
                if self._host_in_cidrs(str(network.network_address), self.exclude_cidrs):
                    return f"target {token} is explicitly excluded from scope"
                if any(network in net or network.subnet_of(net) for net in self._networks(self.cidrs)):
                    continue
                if self.cidrs:
                    return f"target {token} is out of scope (allowed: {', '.join(self.cidrs)})"
                continue
            rejection = self.check_host(host)
            if rejection is not None:
                return rejection
        return None

    def checker(self):
        """Scope checker callable for ToolExecutor.

        Blocks http_request on URL and run_command on embedded
        host/CIDR tokens; read/write file and submit_result are not
        network targets.
        """
        def check(name: str, args: dict[str, Any]) -> str | None:
            if name in ("http_request", "http_probe", "screenshot"):
                return self.check_url(args.get("url", ""))
            if name in ("port_scan", "nuclei_scan"):
                # check_command, not check_host: scan targets may be CIDRs
                # (10.0.0.0/24), which check_command validates as networks.
                return self.check_command(args.get("target", ""))
            if name == "run_command":
                return self.check_command(args.get("command", ""))
            return None

        return check
