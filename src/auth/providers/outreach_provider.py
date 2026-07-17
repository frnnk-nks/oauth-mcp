"""
Provides the Outreach OAuth 2.0 provider (authorization code flow). Serves as an
abstraction layer to app logic.

The Outreach app must list {origin}/auth/callback as a redirect URI. Outreach requires
an https redirect URI, so SERVER_ORIGIN_PROXY should point at an https origin.
"""

import json
import os
import time
from typing import Dict, Optional, Sequence
from urllib.parse import urlencode, urlparse, parse_qs
import httpx
from dotenv import load_dotenv
from auth.providers.provider import OAuthProvider
from auth.tokens.outreach_token import OutreachToken

load_dotenv()
OUTREACH_CLIENT_ID = os.getenv('OUTREACH_CLIENT_ID')
OUTREACH_CLIENT_SECRET = os.getenv('OUTREACH_CLIENT_SECRET')
OUTREACH_OAUTH_BASE_URL = os.getenv('OUTREACH_OAUTH_BASE_URL', 'https://api.outreach.io/oauth')
OUTREACH_LOCAL_TOKEN_PATH = os.getenv('OUTREACH_LOCAL_TOKEN_PATH', './secrets/outreach_token.json')


class OutreachProvider(OAuthProvider):
    """
    Outreach-based OAuth 2.0 provider for application logic.

    Primarily used for local dev testing.
    """

    @property
    def name(self):
        return "outreach"

    @property
    def token_url(self):
        return f"{OUTREACH_OAUTH_BASE_URL}/token"

    def _build_token(self, data: Dict) -> OutreachToken:
        return OutreachToken(
            data,
            token_url=self.token_url,
            client_id=OUTREACH_CLIENT_ID,
            client_secret=OUTREACH_CLIENT_SECRET,
        )

    def _get_stored_token(self, principal_id) -> Optional[OutreachToken]:
        if os.path.exists(OUTREACH_LOCAL_TOKEN_PATH):
            with open(OUTREACH_LOCAL_TOKEN_PATH) as token_file:
                return self._build_token(json.load(token_file))

        return None

    def _save_token(self, token: OutreachToken):
        os.makedirs(os.path.dirname(os.path.abspath(OUTREACH_LOCAL_TOKEN_PATH)), exist_ok=True)
        with open(OUTREACH_LOCAL_TOKEN_PATH, 'w') as token_file:
            token_file.write(token.to_json())

    def invalidate_access_token(self, token: OutreachToken):
        """
        Force the next tool call through a refresh (e.g. after the API returns 401
        before the local expiry was reached).
        """
        token.data['issued_at'] = 0
        self._save_token(token)

    def get_access_token(self, principal_id: str, scopes: Sequence[str]) -> Optional[OutreachToken]:
        """
        Get valid token or return None.
        """
        token = self._get_stored_token(principal_id)
        if token is None or not token.has_scopes(scopes):
            return None
        if token.is_valid and not token.is_stale:
            return token

        # refresh if we can, otherwise fallback on re-auth. Outreach rotates refresh
        # tokens, so the refreshed token must be saved before it is used.
        try:
            if token.can_refresh:
                token.refresh()
                self._save_token(token)
                return token
        except Exception:
            # refresh failed (revoked/expired refresh token) — fallback on re-auth
            pass

        return None

    def generate_auth_url(
        self,
        scopes: Sequence[str],
        elicitation_id: str,
        proxy_origin: str,
        trailing_slash=False,
        **auth_kwargs
    ):
        if not OUTREACH_CLIENT_ID or not OUTREACH_CLIENT_SECRET:
            raise RuntimeError("OUTREACH_CLIENT_ID and OUTREACH_CLIENT_SECRET must be set")

        fmt = "{}/auth/callback/" if trailing_slash else "{}/auth/callback"
        redirect_uri = fmt.format(proxy_origin)
        params = {
            'response_type': 'code',
            'client_id': OUTREACH_CLIENT_ID,
            'redirect_uri': redirect_uri,
            'scope': ' '.join(scopes),
            'state': elicitation_id,
            **auth_kwargs,
        }
        auth_url = f"{OUTREACH_OAUTH_BASE_URL}/authorize?{urlencode(params)}"

        return {
            'id': elicitation_id,
            'provider': self.name,
            'auth_url': auth_url,
            'redirect_uri': redirect_uri,
            'scopes': scopes,
            'state': elicitation_id
        }

    def finish_auth(self, provider_state: Dict, uri):
        query = parse_qs(urlparse(uri).query)
        if 'error' in query:
            description = query.get('error_description', query['error'])[0]
            raise RuntimeError(f"Outreach authorization failed: {description}")
        if query.get('state', [None])[0] != provider_state['state']:
            raise RuntimeError("OAuth state mismatch on Outreach callback")

        response = httpx.post(self.token_url, data={
            'grant_type': 'authorization_code',
            'code': query['code'][0],
            'client_id': OUTREACH_CLIENT_ID,
            'client_secret': OUTREACH_CLIENT_SECRET,
            'redirect_uri': provider_state['redirect_uri'],
        })
        response.raise_for_status()
        payload = response.json()

        token = self._build_token({
            'access_token': payload['access_token'],
            'refresh_token': payload.get('refresh_token'),
            'redirect_uri': provider_state['redirect_uri'],
            'scopes': payload['scope'].split() if payload.get('scope') else list(provider_state['scopes']),
            'issued_at': int(payload.get('created_at', time.time())),
            'expires_in': int(payload.get('expires_in', 7200)),
        })
        self._save_token(token)


def create_outreach_provider():
    return OutreachProvider()
