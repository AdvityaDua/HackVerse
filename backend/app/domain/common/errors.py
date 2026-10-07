class DomainError(Exception):
    """Base error for domain and application policy failures."""


class AuthorizationError(DomainError):
    """Raised when an actor cannot perform an action."""


class ValidationError(DomainError):
    """Raised when a domain invariant is violated."""
