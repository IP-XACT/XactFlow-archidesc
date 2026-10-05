from pathlib import Path

import pytest
from xactflow import Library

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def library() -> Library:
    return Library.scan(FIXTURES / "library")
