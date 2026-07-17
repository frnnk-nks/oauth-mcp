"""
Provides the Salesforce OAuth 2.0 provider (web server flow). Serves as an abstraction
layer to app logic.

Point SALESFORCE_LOGIN_HOST at https://test.salesforce.com for sandbox orgs or at a
My Domain URL. The connected app must list {origin}/auth/callback as a callback URL.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Dict, Optional, Sequence
from urllib.parse import urlencode, urlparse, parse_qs
import httpx
from dotenv import load_dotenv
from auth.providers.provider import OAuthProvider
from auth.tokens.salesforce_token import SalesforceToken

load_dotenv()
SALESFORCE_CLIENT_ID = os.getenv('SALESFORCE_CLIENT_ID')
SALESFORCE_CLIENT_SECRET = os.getenv('SALESFORCE_CLIENT_SECRET')
SALESFORCE_LOGIN_HOST = os.getenv('SALESFORCE_LOGIN_HOST', 'https://login.salesforce.com')
SALESFORCE_LOCAL_TOKEN_PATH = os.getenv('SALESFORCE_LOCAL_TOKEN_PATH', './secrets/salesforce_token.json')
SALESFORCE_TOKEN_TTL_SECONDS = int(os.getenv('SALESFORCE_TOKEN_TTL_SECONDS', '1800'))


class SalesforceProvider(OAuthProvider):
    """
    Salesforce-based OAuth 2.0 provider for application logic.

    Primarily used for local dev testing.
    """

    @property
    def name(self):
        return "salesforce"

    @property
    def token_url(self):
        return f"{SALESFORCE_LOGIN_HOST}/services/oauth2/token"

    def _build_token(self, data: Dict) -> SalesforceToken:
        return SalesforceToken(
            data,
            token_url=self.token_url,
            client_id=SALESFORCE_CLIENT_ID,
            client_secret=SALESFORCE_CLIENT_SECRET,
            ttl_seconds=SALESFORCE_TOKEN_TTL_SECONDS,
        )

    def _get_stored_token(self, principal_id) -> Optional[SalesforceToken]:
        if os.path.exists(SALESFORCE_LOCAL_TOKEN_PATH):
            with open(SALESFORCE_LOCAL_TOKEN_PATH) as token_file:
                return self._build_token(json.load(token_file))

        return None

    def _save_token(self, token: SalesforceToken):
        os.makedirs(os.path.dirname(os.path.abspath(SALESFORCE_LOCAL_TOKEN_PATH)), exist_ok=True)
        with open(SALESFORCE_LOCAL_TOKEN_PATH, 'w') as token_file:
            token_file.write(token.to_json())

    def invalidate_access_token(self, token: SalesforceToken):
        """
        Force the next tool call through a refresh (e.g. after Salesforce kills the
        session server-side and the API returns 401).
        """
        token.data['issued_at'] = 0
        self._save_token(token)

    def get_access_token(self, principal_id: str, scopes: Sequence[str]) -> Optional[SalesforceToken]:
        """
        Get valid token or return None.
        """
        token = self._get_stored_token(principal_id)
        if token is None or not token.has_scopes(scopes):
            return None
        if token.is_valid and not token.is_stale:
            return token

        # refresh if we can, otherwise fallback on re-auth
        try:
            if token.can_refresh:
                token.refresh()
                self._save_token(token)
                return token
        except Exception:
            # refresh failed (revoked/invalid refresh token) — fallback on re-auth
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
        if not SALESFORCE_CLIENT_ID or not SALESFORCE_CLIENT_SECRET:
            raise RuntimeError("SALESFORCE_CLIENT_ID and SALESFORCE_CLIENT_SECRET must be set")

        fmt = "{}/auth/callback/" if trailing_slash else "{}/auth/callback"
        redirect_uri = fmt.format(proxy_origin)

        # PKCE (S256) — required by Salesforce External Client Apps by default
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode('ascii')).digest()
        ).rstrip(b'=').decode('ascii')

        params = {
            'response_type': 'code',
            'client_id': SALESFORCE_CLIENT_ID,
            'redirect_uri': redirect_uri,
            'scope': ' '.join(scopes),
            'state': elicitation_id,
            'prompt': 'consent',
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
            **auth_kwargs,
        }
        auth_url = f"{SALESFORCE_LOGIN_HOST}/services/oauth2/authorize?{urlencode(params)}"

        return {
            'id': elicitation_id,
            'provider': self.name,
            'auth_url': auth_url,
            'redirect_uri': redirect_uri,
            'scopes': scopes,
            'state': elicitation_id,
            'code_verifier': code_verifier
        }

    def finish_auth(self, provider_state: Dict, uri):
        query = parse_qs(urlparse(uri).query)
        if 'error' in query:
            description = query.get('error_description', query['error'])[0]
            raise RuntimeError(f"Salesforce authorization failed: {description}")
        if query.get('state', [None])[0] != provider_state['state']:
            raise RuntimeError("OAuth state mismatch on Salesforce callback")

        token_request = {
            'grant_type': 'authorization_code',
            'code': query['code'][0],
            'client_id': SALESFORCE_CLIENT_ID,
            'client_secret': SALESFORCE_CLIENT_SECRET,
            'redirect_uri': provider_state['redirect_uri'],
        }
        if provider_state.get('code_verifier'):
            token_request['code_verifier'] = provider_state['code_verifier']

        response = httpx.post(self.token_url, data=token_request)
        response.raise_for_status()
        payload = response.json()

        token = self._build_token({
            'access_token': payload['access_token'],
            'refresh_token': payload.get('refresh_token'),
            'instance_url': payload['instance_url'],
            'scopes': payload['scope'].split() if payload.get('scope') else list(provider_state['scopes']),
            'issued_at': int(time.time()),
            'expires_in': SALESFORCE_TOKEN_TTL_SECONDS,
        })
        self._save_token(token)


def create_salesforce_provider():
    return SalesforceProvider()
