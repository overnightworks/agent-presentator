"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""


def main() -> None:
    """Refuse to start, because nothing is wired into the composition root yet."""
    raise NotImplementedError
