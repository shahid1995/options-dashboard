"""Shared underlying-symbol normalization for the paper-entry boundary.

Every layer that validates the request's underlying — the HTTP request
schema, the candidate producer, the sanctioned bridge and the execution
choke point — must agree on what "the same underlying" means, or a
case/whitespace difference could be rejected at one boundary and accepted
at another.  This module is dependency-free by design so both ``app.schemas``
and the service layer can import it without an import cycle (``app.services.
paper_execution`` imports ``app.schemas``).
"""


def normalize_underlying(symbol) -> str:
    """The canonical comparison form of a request/leg underlying symbol.

    Upper-cased and stripped; the same normalization the execution service
    has always applied to the top-level request symbol
    (``app.services.paper_execution.execute_strategy``), now shared so every
    boundary compares identically.
    """
    return str(symbol).strip().upper()
