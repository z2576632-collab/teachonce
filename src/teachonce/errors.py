"""TeachOnce exception hierarchy.

Every error message must tell the caller how to fix the problem.
"""

from __future__ import annotations


class TeachOnceError(Exception):
    """Base class for all TeachOnce errors."""


class NotFoundError(TeachOnceError):
    """Requested entity does not exist."""


class SchemaValidationError(TeachOnceError):
    """Data does not match the expected structure."""


class SensitiveContentError(TeachOnceError):
    """Input appears to contain secrets (passwords / tokens / cookies).

    TeachOnce never stores credentials. Remove the sensitive fragment
    from the input and retry.
    """


class LLMError(TeachOnceError):
    """LLM call failed or returned unusable output."""


class LLMNotConfiguredError(LLMError):
    """No LLM API key configured.

    Set TEACHCONE_LLM_API_KEY (and optionally TEACHCONE_LLM_BASE_URL /
    TEACHCONE_LLM_MODEL), then retry. TeachOnce never fakes LLM output
    when no provider is configured.
    """


class VersionStateError(TeachOnceError):
    """A skill-version state transition is not allowed (approve / rollback misuse).

    History is append-only: refuse to approve a non-existent version,
    roll back to a candidate, or mutate an existing version in place.
    """
