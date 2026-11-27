"""Ingestors: fill the graph from tool output without an LLM.

Cold start problem: an operator has an nmap XML and a nuclei JSONL
from last week's scan. Ingesting them as facts costs nothing and gives
the agent something to reason over before it runs a single command.
"""
from frex.dispatcher.ingest.nmap import ingest_nmap
from frex.dispatcher.ingest.nuclei import ingest_nuclei

INGESTORS = {
    "nmap": ingest_nmap,
    "nuclei": ingest_nuclei,
}

__all__ = ["INGESTORS", "ingest_nmap", "ingest_nuclei"]
