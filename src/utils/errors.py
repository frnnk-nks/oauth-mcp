"""
Construction of custom errors.
"""

class OAuthRequiredError(RuntimeError):
    def __init__(self, message: str, elicitation_id: str):
        self.message = message
        self.elicitation_id = elicitation_id

class MethodNotFoundError(RuntimeError):
    pass

class ScopesNotFoundError(RuntimeError):
    pass

class RetryableApiError(RuntimeError):
    """Transient API failure (network error, 429, 5xx) — safe to retry."""
    pass

class ApiRequestError(RuntimeError):
    """Non-retryable API failure; message includes capped response text."""
    pass
