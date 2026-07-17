"""
File-backed OAuth 2.0 bearer token shared by REST providers (Salesforce, Outreach).

Holds a plain dict of token fields and implements the common expiry/staleness logic.
Concrete tokens implement `refresh` against their provider's token endpoint.
"""

import json
import time
from typing import Any, Dict, Optional, Sequence
from .auth_token import OAuthToken


class BearerToken(OAuthToken):
    """
    Generic OAuth 2.0 bearer token backed by a plain dict.

    Expected fields: access_token, refresh_token, scopes, issued_at (epoch seconds),
    expires_in (seconds). Provider-specific extras (e.g. instance_url) ride along in
    the same dict.
    """
    def __init__(self, data: Dict[str, Any], stale_buffer_seconds: int = 300):
        self.data = data
        self.stale_buffer_seconds = stale_buffer_seconds

    @property
    def access_token(self) -> Optional[str]:
        return self.data.get('access_token')

    @property
    def refresh_token(self) -> Optional[str]:
        return self.data.get('refresh_token')

    @property
    def scopes(self) -> Sequence[str]:
        return self.data.get('scopes', [])

    @property
    def expires_at(self) -> float:
        return self.data.get('issued_at', 0) + self.data.get('expires_in', 0)

    @property
    def is_valid(self):
        return bool(self.access_token) and time.time() < self.expires_at

    @property
    def is_stale(self):
        return time.time() >= self.expires_at - self.stale_buffer_seconds

    @property
    def can_refresh(self):
        return bool(self.refresh_token)

    def has_scopes(self, scopes: Sequence[str]) -> bool:
        return set(scopes) <= set(self.scopes)

    def refresh(self):
        raise NotImplementedError

    def set_creds(self, data: Dict[str, Any]):
        self.data = data

    def present_creds(self) -> Dict[str, Any]:
        return self.data

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2)
