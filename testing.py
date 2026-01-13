"""Test utilities."""

import sys
import warnings


# Suppress unawaited coroutine warnings from AsyncMock in tests.
warnings.filterwarnings("ignore", message="coroutine.*was never awaited")


def run_tests(test_file: str) -> None:
    """Run pytest on a test file with standard flags.

    Usage:
        if __name__ == "__main__":
            from testing import run_tests
            run_tests(__file__)
    """
    import pytest

    sys.exit(
        pytest.main(
            [
                test_file,
                "-v",
                "-s",
                "-W",
                "ignore::pytest.PytestAssertRewriteWarning",
                *sys.argv[1:],
            ]
        )
    )
