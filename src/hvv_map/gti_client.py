"""HVV GTI API (geofox) client.

Credentials, in priority order:
  1. Env vars GTI_USER / GTI_HMAC_SECRET (Docker Compose via .env)
  2. credentials.json in the working directory: {"user": ..., "secret": ...}
"""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

import requests

CREDENTIALS_JSON_PATH = Path("credentials.json")


def _load_credentials():
    env_user = os.environ.get("GTI_USER")
    env_secret = os.environ.get("GTI_HMAC_SECRET")
    if env_user and env_secret:
        return env_user, env_secret

    if CREDENTIALS_JSON_PATH.is_file():
        data = json.loads(CREDENTIALS_JSON_PATH.read_text(encoding="utf-8"))
        user, secret = data.get("user"), data.get("secret")
        if user and secret:
            return user, secret
        raise RuntimeError(
            f"{CREDENTIALS_JSON_PATH.resolve()} needs 'user' and 'secret'."
        )

    raise RuntimeError(
        "No GTI credentials found. Set GTI_USER/GTI_HMAC_SECRET env vars, "
        'or create credentials.json: {"user": ..., "secret": ...}'
    )


class GtiClient:
    def __init__(self, _user: str = None, _secret: str = None):
        if _user is None or _secret is None:
            auto_user, auto_secret = _load_credentials()
            _user = _user or auto_user
            _secret = _secret or auto_secret
        self.gti_user = _user
        self.gti_hmac_secret = _secret
        self.client = requests.Session()

    def _get_signature(self, request_body: str) -> str:
        key = self.gti_hmac_secret.encode("utf-8")
        message = request_body.encode("utf-8")
        digest = hmac.new(key, message, hashlib.sha1).digest()
        return base64.b64encode(digest).decode("utf-8")

    def send(self, endpoint: str, request: dict) -> dict:
        url = "https://gti.geofox.de/gti/public/" + endpoint
        request_body = json.dumps(request)
        headers = {
            "Accept": "application/json",
            "geofox-auth-signature": self._get_signature(request_body),
            "geofox-auth-user": self.gti_user,
            "geofox-auth-type": "HmacSHA1",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        response = self.client.request("POST", url, headers=headers, data=request_body)
        return response.json()
