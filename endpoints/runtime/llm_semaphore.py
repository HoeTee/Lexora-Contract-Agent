"""Compatibility import for the renamed shared model admission controller."""
import sys

from endpoints.runtime import model_limits

# Preserve module identity while callers migrate, including test patches.
sys.modules[__name__] = model_limits
