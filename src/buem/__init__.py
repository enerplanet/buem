import os

# Version: single source of truth is the git tag.
# Fallback chain: setuptools-scm (live from git) → _version.py → hardcoded fallback.
# The live git read avoids stale _version.py when using 'conda develop'.
try:
    from setuptools_scm import get_version as _scm_version
    __version__ = _scm_version()
except (ImportError, LookupError):
    # ImportError: setuptools-scm not installed. LookupError: not a git
    # checkout / no tags found (setuptools-scm's own failure mode).
    try:
        from buem._version import version as __version__
    except ImportError:
        __version__ = "0.1.3"  # keep in sync with pyproject.toml fallback_version


def build_info() -> tuple[str, str]:
    """The release version and git commit this process serves.

    The published image sets BUEM_VERSION and BUEM_COMMIT at build time: it
    carries neither .git nor _version.py, so __version__ there is only the
    fallback. Elsewhere the commit is "unknown" unless BUEM_COMMIT is set.
    """
    return os.environ.get("BUEM_VERSION") or __version__, os.environ.get("BUEM_COMMIT") or "unknown"
