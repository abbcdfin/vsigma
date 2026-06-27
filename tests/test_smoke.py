"""Smoke test: the package imports and exposes a version."""

import vsigma


def test_package_exposes_version() -> None:
    assert isinstance(vsigma.__version__, str)
    assert vsigma.__version__
