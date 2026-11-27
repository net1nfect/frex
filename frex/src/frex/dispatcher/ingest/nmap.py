"""nmap XML → host/port/service facts."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from frex.dispatcher.ingest.base import IngestedFact


def ingest_nmap(xml_data: str) -> list[IngestedFact]:
    root = ET.fromstring(xml_data)
    facts: list[IngestedFact] = []
    for host in root.iter("host"):
        addr_el = host.find("address")
        ip = addr_el.get("addr") if addr_el is not None else None
        if not ip:
            continue
        hostname = None
        hostnames = host.find("hostnames")
        if hostnames is not None:
            hn = hostnames.find("hostname")
            if hn is not None:
                hostname = hn.get("name")
        subject = {"host": ip}
        if hostname:
            subject["hostname"] = hostname

        status_el = host.find("status")
        if status_el is not None and status_el.get("state") == "up":
            facts.append(
                IngestedFact(
                    description=f"host {ip} is up" + (f" ({hostname})" if hostname else ""),
                    subject=subject,
                    match_value=ip,
                )
            )

        ports_el = host.find("ports")
        if ports_el is None:
            continue
        for port in ports_el.iter("port"):
            port_id = port.get("portid")
            protocol = port.get("protocol", "tcp")
            state_el = port.find("state")
            state = state_el.get("state") if state_el is not None else None
            if state != "open":
                continue
            service_el = port.find("service")
            service = (service_el.get("name") if service_el is not None else "") or ""
            product = (service_el.get("product") if service_el is not None else "") or ""
            version = (service_el.get("version") if service_el is not None else "") or ""
            banner = " ".join(x for x in (product, version) if x)
            desc = f"{protocol}/{port_id} open on {ip}"
            if service:
                desc += f" running {service}" + (f" ({banner})" if banner else "")
            facts.append(
                IngestedFact(
                    description=desc,
                    kind="access",
                    subject={**subject, "port": int(port_id), "protocol": protocol,
                             "service": service or None},
                    match_rule="regex",
                    match_value=rf"{protocol}/{port_id}\s+open",
                )
            )
    return facts
