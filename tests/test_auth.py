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


def test_extract_pem_tolerates_notes_and_crlf(rsa_key, tmp_path):
    from kalshi_bot.auth import extract_pem, find_key_id

    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    messy = (
        b"my kalshi key\r\nid: 12345678-abcd-4ef0-9876-0123456789ab\r\n"
        + pem.replace(b"\n", b"\r\n")
        + b"\r\nremember to delete this\r\n"
    )
    assert extract_pem(messy).startswith(b"-----BEGIN RSA PRIVATE KEY-----\n")
    assert b"\r" not in extract_pem(messy)
    assert find_key_id(messy) == "12345678-abcd-4ef0-9876-0123456789ab"
    assert find_key_id(pem) is None
    path = tmp_path / "notes.txt"
    path.write_bytes(messy)
    s = Signer.from_pem_path("abc", path)
    assert "BEGIN PUBLIC KEY" in s.public_key_pem()
    import pytest

    with pytest.raises(ValueError):
        extract_pem(b"nothing here")


def test_extract_pem_handles_utf16_and_bom(rsa_key):
    from kalshi_bot.auth import extract_pem, find_key_id

    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    text = "id 12345678-abcd-4ef0-9876-0123456789ab\r\n" + pem
    for encoded in (text.encode("utf-16"), text.encode("utf-8-sig"), text.encode("utf-16-le")):
        assert extract_pem(encoded).startswith(b"-----BEGIN PRIVATE KEY-----")
        assert find_key_id(encoded) == "12345678-abcd-4ef0-9876-0123456789ab"
        Signer.from_pem("x", encoded)
