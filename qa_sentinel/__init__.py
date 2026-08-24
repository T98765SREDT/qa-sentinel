"""QA Sentinel: dependency-free API regression testing."""

from .config import ConfigError, load_suite
from .runner import SuiteRunner

__all__ = ["ConfigError", "SuiteRunner", "load_suite"]
__version__ = "1.0.0"

