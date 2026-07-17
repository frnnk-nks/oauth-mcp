"""
Salesforce OAuth 2.0 token. Salesforce does not report access-token lifetimes, so a
configurable local TTL decides when to refresh proactively. Refresh tokens are not
rotated by Salesforce on refresh.
"""

import time
import httpx
from typing import Any, Dict, Optional, Sequence
from .bearer_token import BearerToken


class SalesforceToken(BearerToken):
    def __init__(
        self,
        data: Dict[str, Any],
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        ttl_seconds: int
    ):
        super().__init__(data)
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.ttl_seconds = ttl_seconds

    @property
    def instance_url(self) -> Optional[str]:
        return self.data.get('instance_url')

    def has_scopes(self, scopes: Sequence[str]) -> bool:
        # the 'full' scope grants access to every API scope
        return 'full' in self.scopes or super().has_scopes(scopes)

    def refresh(self):
        response = httpx.post(self.token_url, data={
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        })
        response.raise_for_status()
        payload = response.json()

        self.data.update({
            'access_token': payload['access_token'],
            'instance_url': payload.get('instance_url', self.instance_url),
            'scopes': payload['scope'].split() if payload.get('scope') else list(self.scopes),
            'issued_at': int(time.time()),
            'expires_in': self.ttl_seconds,
        })
