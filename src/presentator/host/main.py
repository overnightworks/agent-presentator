"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""


def main() -> None:
    """Run the presentation host until it is asked to stop."""
    raise NotImplementedError
