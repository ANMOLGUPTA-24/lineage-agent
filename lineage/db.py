"""FalkorDB connection. One graph per tenant: the graph name is the isolation boundary."""
import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

DEFAULT_GRAPH = os.getenv("LINEAGE_GRAPH", "python")


def connect() -> FalkorDB:
    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
    )


def graph(name: str = DEFAULT_GRAPH):
    return connect().select_graph(name)
