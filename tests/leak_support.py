"""What a log record or an error shows: the one walk every leak test uses."""

import logging
import traceback
from collections.abc import Iterable, Mapping
from typing import cast


def everything_shown(
    records: Iterable[logging.LogRecord] = (),
    errors: Iterable[BaseException] = (),
) -> str:
    """The text of every leaf the records and errors hold, in `str` and `repr`.

    A record shows its message, `args`, `exc_info`, `exc_text` and `stack_info`;
    an error shows its text, `args`, formatted traceback, `__cause__`,
    `__context__` and group members. Containers are walked iteratively and each
    object once, so a cyclic chain ends.
    """
    pending: list[object] = [*records, *errors]
    visited: set[int] = set()
    leaves: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        leaves.extend((str(current), repr(current)))
        pending.extend(_parts_of(current))
    return "\n".join(leaves)


def _parts_of(value: object) -> list[object]:
    if isinstance(value, logging.LogRecord):
        return [
            value.getMessage(),
            value.args,
            value.exc_info,
            value.exc_text,
            value.stack_info,
        ]
    if isinstance(value, BaseException):
        return _parts_of_error(value)
    if isinstance(value, Mapping):
        entries = cast("Mapping[object, object]", value)
        return [*entries.keys(), *entries.values()]
    if isinstance(value, tuple | list | set | frozenset):
        return list(cast("Iterable[object]", value))
    return []


def _parts_of_error(error: BaseException) -> list[object]:
    formatted = "".join(traceback.format_exception(error))
    members: tuple[object, ...] = (
        tuple(cast("BaseExceptionGroup[BaseException]", error).exceptions)
        if isinstance(error, BaseExceptionGroup)
        else ()
    )
    return [
        error.args,
        error.__cause__,
        error.__context__,
        formatted,
        *members,
    ]
