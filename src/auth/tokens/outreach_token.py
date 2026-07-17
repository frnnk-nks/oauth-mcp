"""
Outreach OAuth 2.0 token. Outreach rotates refresh tokens: every refresh returns a new
refresh_token, so refreshed tokens must be persisted immediately by the provider.
"""

import time
import httpx
from typing import Any, Dict
from .bearer_token import BearerToken


class OutreachToken(BearerToken):
    def __init__(
        self,
        data: Dict[str, Any],
        *,
        token_url: str,
        client_id: str,
        client_secret: str
    ):
        super().__init__(data)
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret

    def refresh(self):
        request_body = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        if self.data.get('redirect_uri'):
            request_body['redirect_uri'] = self.data['redirect_uri']

        response = httpx.post(self.token_url, data=request_body)
        response.raise_for_status()
        payload = response.json()

        self.data.update({
            'access_token': payload['access_token'],
            'refresh_token': payload.get('refresh_token', self.refresh_token),
            'scopes': payload['scope'].split() if payload.get('scope') else list(self.scopes),
            'issued_at': int(payload.get('created_at', time.time())),
            'expires_in': int(payload.get('expires_in', 7200)),
        })
