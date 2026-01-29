"""
Provides implementations for Google OAuth providers. Serves as an abstraction layer to app logic.
"""

import os
import os.path
from typing import Sequence, Union, Optional, Dict
from auth.providers.provider import OAuthProvider, LocalRedirectWSGIApp
from auth.tokens.google_token import GoogleToken
from utils.errors import OAuthRequiredError
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from dotenv import load_dotenv

load_dotenv()
SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_SECRETS_PATH = os.getenv("GOOGLE_SECRETS_PATH")
GOOGLE_LOCAL_TOKEN_PATH = os.getenv("GOOGLE_LOCAL_TOKEN_PATH")


class GoogleProvider(OAuthProvider):
    """
    Google-based OAuth 2.0 provider for appication logic.

    Primarily used for local dev testing.
    """
    def __init__(self):
        self.local_server = None
        self.redirect_app = None

    @property
    def name(self):
        return "google"

    def _get_stored_token(self, principal_id, scopes) -> Optional[GoogleToken]:
        if os.path.exists(GOOGLE_LOCAL_TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(GOOGLE_LOCAL_TOKEN_PATH, scopes)
            return GoogleToken(creds)
        
        return None
    
    def get_access_token(self, principal_id: str, scopes: Sequence[str]) -> Optional[GoogleToken]:
        """
        Get valid token or return None.
        """
        token = self._get_stored_token(principal_id, scopes)
        if token is not None and token.is_valid:
            if not token.is_stale:
                return token
        
        # refresh if we can, otherwise fallback on re-auth
        try:
            if token is not None and token.can_refresh:
                token.refresh()
                with open(GOOGLE_LOCAL_TOKEN_PATH, 'w') as new_token:
                    new_token.write(token.creds.to_json())
            
                return token
        except:
            # log that refresh failed (invalid refresh token)
            pass

        return None

    def generate_auth_url(
        self,
        scopes: Sequence[str],
        elicitation_id: str,  
        proxy_origin: str, 
        trailing_slash=True,
        **auth_kwargs
    ):
        flow = Flow.from_client_secrets_file(GOOGLE_SECRETS_PATH, scopes=scopes)
        fmt = "{}/auth/callback/" if trailing_slash else "{}/auth/callback"
        redirect_uri = fmt.format(proxy_origin, elicitation_id)
        flow.redirect_uri = redirect_uri
        auth_url, state = flow.authorization_url(
            state=elicitation_id,
            access_type='offline',
            prompt='consent',
            **auth_kwargs
        )

        return {
            'id': elicitation_id,
            'provider': self.name,
            'auth_url': auth_url,
            'redirect_uri': redirect_uri,
            'scopes': scopes,
            'state': state
        }
    
    def finish_auth(self, provider_state: Dict, uri):
        # restore flow
        flow = Flow.from_client_secrets_file(
            client_secrets_file=GOOGLE_SECRETS_PATH,
            scopes=provider_state['scopes'],
            state=provider_state['state']
        )
        flow.redirect_uri = provider_state['redirect_uri']

        # Note: using https here because oauthlib is very picky that
        # OAuth 2.0 should only occur over https.
        if uri[:5] != "https":
            uri = "https" + uri[4:]

        authorization_response = uri
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials

        with open(GOOGLE_LOCAL_TOKEN_PATH, 'w') as new_token:
            new_token.write(creds.to_json())


def create_google_provider():
    return GoogleProvider()


def main():
    pass

if __name__ == "__main__":
    main()
    pass


