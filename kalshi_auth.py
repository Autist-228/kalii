import base64
import datetime

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key_from_file(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def load_private_key_from_string(key_pem: str):
    return serialization.load_pem_private_key(
        key_pem.encode("utf-8"), password=None, backend=default_backend()
    )


def create_signature(private_key, timestamp_ms: str, method: str, path: str) -> str:
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def get_auth_headers(private_key, api_key_id: str, method: str, path: str) -> dict:
    timestamp_ms = str(int(datetime.datetime.now().timestamp() * 1000))
    signature = create_signature(private_key, timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type": "application/json",
    }
