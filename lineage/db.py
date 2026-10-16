"""FalkorDB connection. One graph per tenant: the graph name is the isolation boundary.

A tenant name is resolved through an alias key, `lineage:active:<tenant>`, that names the
physical graph serving it. `make sync` builds a fresh graph beside the live one and then flips
the alias with a single SET, so readers see the old graph or the new one, never a half-built
one. With no alias set, the tenant name is the graph name.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

DEFAULT_GRAPH = os.getenv("LINEAGE_GRAPH", "python")
ALIAS = "lineage:active:{}"


@lru_cache(maxsize=1)
def connect() -> FalkorDB:
    # One client per process; its redis connection pool is thread-safe, so concurrent requests
    # share connections instead of opening one per query.
    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
    )


def resolve(tenant: str) -> str:
    name = connect().connection.get(ALIAS.format(tenant))
    return name.decode() if isinstance(name, bytes) else (name or tenant)


def graph(name: str = DEFAULT_GRAPH):
    return connect().select_graph(resolve(name))
