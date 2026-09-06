"""Smoke test for the composition root."""

import pytest

from presentator.host import main


def test_the_composition_root_refuses_to_run_while_it_is_empty() -> None:
    with pytest.raises(NotImplementedError):
        main.main()
