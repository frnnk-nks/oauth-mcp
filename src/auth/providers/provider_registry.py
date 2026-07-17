"""
Registry for different providers, to support dynamic retrieval of providers.

Providers should be one-time initialized.
"""

from auth.providers.salesforce_provider import create_salesforce_provider
from auth.providers.outreach_provider import create_outreach_provider
from auth.providers.provider import OAuthProvider

SALESFORCE_PROVIDER = create_salesforce_provider()
OUTREACH_PROVIDER = create_outreach_provider()

PROVIDER_REGISTRY = {
    'salesforce': SALESFORCE_PROVIDER,
    'outreach': OUTREACH_PROVIDER
}

def get_provider(provider_name: str) -> OAuthProvider:
    if PROVIDER_REGISTRY.get(provider_name) is None:
        raise RuntimeError("provider not found")

    return PROVIDER_REGISTRY[provider_name]
