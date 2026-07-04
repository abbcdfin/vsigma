"""The kata validator: run the candidate code against the tests, deterministically.

`make_test_validator(tests)` returns a vsigma `Validator` that executes the
candidate implementation plus the given assert-based tests in a fresh
`sys.executable` subprocess (timeout-bounded) and reports the failure output
as the critique. This is the Reflexion insight in its natural habitat: code is
the domain where an objective critic exists for free.

Honest limitation: a test-running critic makes the loop converge to "passes
the tests", not "is correct" — the critique is only as good as the tests.

Note the candidate code is *executed*; that is the point of the kata, but run
it only on tasks/models you trust as you would any generated code.
"""

from __future__ import annotations

import re
import subprocess
import sys

from vsigma import Validator

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str:
    """Pull the code out of a model reply — fenced block if present, else as-is."""
    blocks = _FENCE.findall(reply)
    return blocks[0].strip() if blocks else reply.strip()


def make_test_validator(tests: str, *, timeout: float = 10.0) -> Validator:
    """A deterministic validator: candidate + tests in a subprocess.

    Returns None when every assert passes; otherwise the tail of the failure
    output (the traceback the agent revises against).
    """

    def validate(candidate: str) -> str | None:
        program = extract_code(candidate) + "\n\n" + tests
        try:
            proc = subprocess.run(
                [sys.executable, "-"],
                input=program,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"tests timed out after {timeout}s"
        if proc.returncode == 0:
            return None
        failure = (proc.stderr or proc.stdout).strip()
        return failure[-800:] or f"tests exited with code {proc.returncode}"

    return validate


#: The demo kata: run-length encoding.
RLE_TASK = (
    "Write a Python function `rle(s: str) -> str` that run-length encodes a "
    "string: rle('aaabcc') == 'a3b1c2', rle('') == ''. Reply with only the code."
)

RLE_TESTS = """\
assert rle("") == "", f"rle('') -> {rle('')!r}"
assert rle("a") == "a1", f"rle('a') -> {rle('a')!r}"
assert rle("aaabcc") == "a3b1c2", f"rle('aaabcc') -> {rle('aaabcc')!r}"
print("all tests passed")
"""
