import base64
import hashlib
import hmac
import json


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, secret: str) -> str:
    if not secret:
        raise ValueError("empty token secret")
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16])
    return f"{body}.{sig}"


def verify(token: str, secret: str) -> dict:
    try:
        body, sig = token.split(".")
        want = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16])
        if not secret or not hmac.compare_digest(sig, want):
            raise ValueError("bad signature")
        return json.loads(_unb64(body))
    except ValueError:
        raise
    except Exception as e:  # malformed base64 / json / split
        raise ValueError(f"bad token: {e}") from e


def visitor(profile_id: int, secret: str) -> str:
    """Identifies a browser as this profile (signup response, #om= fragment after a mail click)."""
    return sign({"p": profile_id, "a": "id"}, secret)


def visitor_profile(token: str, secret: str) -> int | None:
    try:
        data = verify(token, secret)
    except ValueError:
        return None
    p = data.get("p")
    return p if data.get("a") == "id" and isinstance(p, int) and not isinstance(p, bool) else None
