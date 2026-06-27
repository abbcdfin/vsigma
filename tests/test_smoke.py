"""Smoke test: the package imports and exposes a version.

Replaced by real tests as Phase 1 building blocks land.
"""

import vsigma


def test_package_exposes_version() -> None:
    assert isinstance(vsigma.__version__, str)
    assert vsigma.__version__
