"""Starting the local web interface and opening it in the browser."""

from __future__ import annotations

import socket
import webbrowser
from collections.abc import Mapping
from pathlib import Path

import uvicorn

from biblio_search.decs import DecsClient, DecsConfig
from biblio_search.eutils import EutilsClient, EutilsConfig
from biblio_search.models import Database
from biblio_search.openalex import OpenAlexClient, OpenAlexConfig
from biblio_search.web.app import BackendFactory, create_app

HOST = "127.0.0.1"


def bind_local(port: int) -> socket.socket:
    """Listen on ``port`` of 127.0.0.1 (0 picks any free port); never on other interfaces."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


def serve(
    config: EutilsConfig,
    *,
    port: int = 0,
    open_browser: bool = True,
    initial_question: Mapping[str, object] | None = None,
    screening_path: Path | None = None,
) -> None:
    """Run the interface until it is stopped with Ctrl+C."""

    def open_client() -> EutilsClient:
        return EutilsClient(config)

    def open_openalex() -> OpenAlexClient:
        return OpenAlexClient(OpenAlexConfig.from_env())

    decs_config = DecsConfig.from_env()

    def open_decs() -> DecsClient:
        if decs_config is None:
            raise RuntimeError("DeCS is only opened when DECS_API_KEY is set")
        return DecsClient(decs_config)

    backends: dict[Database, BackendFactory] = {Database.OPENALEX: open_openalex}
    app = create_app(
        open_client,
        initial_question,
        backends,
        open_decs if decs_config is not None else None,
        screening_path,
    )
    sock = bind_local(port)
    try:
        url = f"http://{HOST}:{sock.getsockname()[1]}/"
        print(f"biblio-search is running at {url} (press Ctrl+C to stop)")
        # El socket ya escucha: si el navegador llega antes que uvicorn, espera en la cola.
        if open_browser:
            webbrowser.open(url)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        server.run(sockets=[sock])
    finally:
        sock.close()
