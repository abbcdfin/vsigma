"""The code-kata worked example — deterministic test-running validator."""

from examples.code_kata.demo import run_demo
from examples.code_kata.kata import RLE_TESTS, extract_code, make_test_validator


def test_extract_code_strips_fences() -> None:
    assert extract_code("```python\ndef f():\n    pass\n```") == "def f():\n    pass"
    assert extract_code("def g():\n    pass") == "def g():\n    pass"


def test_validator_accepts_correct_code() -> None:
    validate = make_test_validator(RLE_TESTS)
    correct = (
        "def rle(s):\n"
        "    import itertools\n"
        "    return ''.join(f'{c}{len(list(g))}' for c, g in itertools.groupby(s))"
    )
    assert validate(correct) is None


def test_validator_returns_traceback_as_critique() -> None:
    validate = make_test_validator(RLE_TESTS)
    buggy = "def rle(s):\n    return s[0]"  # IndexError on rle("")
    critique = validate(buggy)
    assert critique is not None
    assert "IndexError" in critique


def test_demo_converges_offline() -> None:
    run = run_demo()
    assert run.status == "done"
    assert run.result is not None and "def rle" in run.result
    # The trajectory shows the loop: act -> critic (reject) -> act -> critic (pass).
    nodes = [event.node for event in run.trajectory]
    assert nodes == ["act", "critic", "act", "critic"]
