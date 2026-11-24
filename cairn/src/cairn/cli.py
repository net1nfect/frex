from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--server", default="http://127.0.0.1:8000", show_default=True, help="Cairn server URL")
@click.option("--project", "project_id", required=True, help="Project id to ingest into")
@click.option("--type", "ingest_type", required=True, type=click.Choice(["nmap", "nuclei"]), help="Ingestor type")
@click.option("--file", "input_file", required=True, type=click.Path(exists=True, dir_okay=False), help="Tool output file")
def ingest(server: str, project_id: str, ingest_type: str, input_file: Path):
    """Import tool output as facts without an LLM."""
    import requests

    from cairn.dispatcher.ingest import INGESTORS
    from cairn.dispatcher.ingest.base import post_facts
    from cairn.dispatcher.protocol.client import CairnClient

    data = Path(input_file).read_text()
    facts = INGESTORS[ingest_type](data)
    if not facts:
        click.echo("nothing to ingest")
        return
    client = CairnClient(server)
    posted, failed = post_facts(client, project_id, facts)
    click.echo(f"ingested {posted} facts ({failed} failed) from {ingest_type}")
