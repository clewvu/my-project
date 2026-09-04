"""RSA-PSS request signing for the Kalshi Trade API v2.

Every authenticated request carries three headers:

    KALSHI-ACCESS-KEY        the API key id
    KALSHI-ACCESS-TIMESTAMP  unix time in milliseconds
    KALSHI-ACCESS-SIGNATURE  base64(RSA-PSS-SHA256(timestamp + METHOD + path))

The signed path is the full URL path including the ``/trade-api/v2`` prefix and
excluding any query string.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"


class Signer:
    """Signs requests with an RSA private key using PSS padding."""

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        if not key_id:
            raise ValueError("key_id is required")
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_pem_path(cls, key_id: str, path: str | Path) -> Signer:
        pem = Path(path).expanduser().read_bytes()
        return cls.from_pem(key_id, pem)

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes | str) -> Signer:
        if isinstance(pem, str):
            pem = pem.encode("utf-8")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi private key must be an RSA key")
        return cls(key_id, key)

    @staticmethod
    def message(timestamp_ms: int | str, method: str, path: str) -> bytes:
        """The exact byte string that gets signed."""
        path = path.split("?", 1)[0]
        return f"{timestamp_ms}{method.upper()}{path}".encode()

    def sign(self, timestamp_ms: int | str, method: str, path: str) -> str:
        signature = self._private_key.sign(
            self.message(timestamp_ms, method, path),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: ts,
            HEADER_SIGNATURE: self.sign(ts, method, path),
        }

    def public_key_pem(self) -> str:
        """Handy for verifying in tests or sanity-checking which key is loaded."""
        return (
            self._private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )
