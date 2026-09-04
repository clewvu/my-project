import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_bot.auth import HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP, Signer


def test_message_format_strips_query_and_uppercases_method():
    msg = Signer.message(1700000000000, "get", "/trade-api/v2/markets?limit=5")
    assert msg == b"1700000000000GET/trade-api/v2/markets"


def test_signature_verifies_with_public_key(signer, rsa_key):
    ts = 1700000000000
    sig = signer.sign(ts, "POST", "/trade-api/v2/portfolio/orders")
    rsa_key.public_key().verify(
        base64.b64decode(sig),
        Signer.message(ts, "POST", "/trade-api/v2/portfolio/orders"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_headers_contain_all_three(signer):
    h = signer.headers("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=123)
    assert h[HEADER_KEY] == "test-key-id"
    assert h[HEADER_TIMESTAMP] == "123"
    assert h[HEADER_SIGNATURE]


def test_from_pem_roundtrip(rsa_key, tmp_path):
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "k.pem"
    path.write_bytes(pem)
    s = Signer.from_pem_path("abc", path)
    assert s.key_id == "abc"
    assert "BEGIN PUBLIC KEY" in s.public_key_pem()
