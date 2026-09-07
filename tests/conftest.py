import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.auth import Signer


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(rsa_key):
    return Signer("test-key-id", rsa_key)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Every test runs in its own directory, so a default path such as
    ``state/alerts.jsonl`` or ``state/decisions.jsonl`` can never land in the
    repository's real ``state`` folder, where the live loop keeps its records."""
    monkeypatch.chdir(tmp_path)
    yield
