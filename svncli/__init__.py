"""Polarion SVN CLI — sync files with Polarion SVN when direct SVN access is not available."""

from importlib.metadata import version

from .api import PolarionSVNClient
from .client import AuthenticationError, NotFoundError, SVNWebClientError
from .models import RemoteItem, SyncAction, SyncOp

__version__ = version("svncli")

__all__ = [
    "PolarionSVNClient",
    "SVNWebClientError",
    "AuthenticationError",
    "NotFoundError",
    "RemoteItem",
    "SyncAction",
    "SyncOp",
    "__version__",
]
