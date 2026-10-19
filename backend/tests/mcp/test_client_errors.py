"""Transport failures name their cause instead of 'unhandled errors in a TaskGroup'."""

from __future__ import annotations

import httpx2

from opsbridge.mcp_services.client import describe_error


def test_nested_exception_groups_name_their_leaf_errors() -> None:
    inner = ExceptionGroup("inner", [ConnectionResetError("reset by peer")])
    outer = ExceptionGroup(
        "unhandled errors in a TaskGroup", [httpx2.ReadTimeout("timed out"), inner]
    )
    text = describe_error(outer)
    assert text == (
        "ExceptionGroup of 2: ReadTimeout: timed out; ConnectionResetError: reset by peer"
    )


def test_plain_errors_keep_type_and_message() -> None:
    assert describe_error(TimeoutError()) == "TimeoutError: "
    assert describe_error(ValueError("bad")) == "ValueError: bad"
