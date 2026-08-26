"""Compatibility re-export. Prefer apps.core.authentication."""

from apps.core.authentication import CsrfExemptSessionAuthentication

__all__ = ["CsrfExemptSessionAuthentication"]
