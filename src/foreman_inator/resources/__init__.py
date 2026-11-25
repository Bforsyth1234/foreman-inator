"""Local machine resources the worker can look at (today: models Ollama has loaded).

See ``resources.ollama`` for the public API: ``loaded_models``, ``OllamaWatch``,
``LoadedModel`` and ``Unreachable``.
"""

from foreman_inator.resources.ollama import (
    LoadedModel,
    OllamaWatch,
    Unreachable,
    loaded_models,
)

__all__ = ["LoadedModel", "OllamaWatch", "Unreachable", "loaded_models"]
