"""A pluginized Cordis agent backed by the real DeepSeek API."""

from .llm import DeepSeekAPI
from .plugins import bundle
from .tools import Workspace

__all__ = ["DeepSeekAPI", "Workspace", "bundle"]
