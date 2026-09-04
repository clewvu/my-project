import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.auth import Signer


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(rsa_key):
    return Signer("test-key-id", rsa_key)
