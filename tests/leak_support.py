"""What a log record or an error shows: the one walk every leak test uses."""

import logging
import traceback
from collections.abc import Callable, Iterable, Mapping
from typing import cast


def everything_shown(
    records: Iterable[logging.LogRecord] = (),
    errors: Iterable[BaseException] = (),
) -> str:
    """The text of every leaf the records and errors hold, in `str` and `repr`.

    A record shows its message, `args`, `exc_info`, `exc_text` and `stack_info`;
    an error shows its text, `args`, formatted traceback, `__cause__`,
    `__context__` and group members. Containers are walked iteratively and each
    object once, kept alive so no id is reused, so a cyclic chain ends. A
    container is never formatted whole, because that recurses through its
    children; only its leaves are. Where an object's own text cannot be formed
    at a depth, its parts still are walked.
    """
    pending: list[object] = [*records, *errors]
    visited: dict[int, object] = {}
    leaves: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited[id(current)] = current
        pending.extend(_parts_of(current))
        if not _is_container(current):
            leaves.extend([*_shown(str, current), *_shown(repr, current)])
    return "\n".join(leaves)


def _is_container(value: object) -> bool:
    return isinstance(value, Mapping | tuple | list | set | frozenset)


def _shown[Subject](render: Callable[[Subject], str], subject: Subject) -> list[str]:
    try:
        return [render(subject)]
    except RecursionError:
        return []


def _parts_of(value: object) -> list[object]:
    if isinstance(value, logging.LogRecord):
        return [
            *_shown(logging.LogRecord.getMessage, value),
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
    formatted = _shown(_formatted, error)
    members: tuple[object, ...] = (
        tuple(cast("BaseExceptionGroup[BaseException]", error).exceptions)
        if isinstance(error, BaseExceptionGroup)
        else ()
    )
    return [
        error.args,
        error.__cause__,
        error.__context__,
        *formatted,
        *members,
    ]


def _formatted(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))
