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
import re
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"


PEM_BLOCK = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
KEY_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def extract_pem(text: bytes | str) -> bytes:
    """The private-key block inside ``text``, whatever else the file contains.

    Kalshi's download is a clean PEM, but people paste keys into notes files
    with the key id and other text around them. Line endings are normalised.
    """
    data = text.encode("utf-8") if isinstance(text, str) else _to_utf8(text)
    match = PEM_BLOCK.search(data)
    if not match:
        raise ValueError("no '-----BEGIN ... PRIVATE KEY-----' block found in the key file")
    return match.group(0).replace(b"\r\n", b"\n").replace(b"\r", b"\n") + b"\n"


def _to_utf8(data: bytes) -> bytes:
    """Notepad and PowerShell sometimes save UTF-16 (with a byte-order mark); normalise it."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16").encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:]
    if len(data) > 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le", "replace").encode("utf-8")
    return data


def find_key_id(text: bytes | str) -> str | None:
    """A Kalshi key id (a UUID) appearing in ``text`` outside the PEM block, if any."""
    data = _to_utf8(text).decode("utf-8", "replace") if isinstance(text, bytes) else text
    outside = PEM_BLOCK.sub(b"", data.encode("utf-8")).decode("utf-8", "replace")
    match = KEY_ID.search(outside)
    return match.group(0) if match else None


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
        key = serialization.load_pem_private_key(extract_pem(pem), password=None)
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
