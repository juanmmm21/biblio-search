"""Local web interface of biblio-search."""

from biblio_search.web.app import create_app
from biblio_search.web.server import serve

__all__ = ["create_app", "serve"]
