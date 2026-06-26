"""Learning / experience brain (shadow mode).

Observe-only: records trade entries/outcomes and surfaces a historical prior
plus reflection rules into the analyst prompt. Does NOT change confidence or
gate trades. Flip to active influence later behind a flag.
"""

from src.learning.experience_store import ExperienceStore, get_experience_store

__all__ = ["ExperienceStore", "get_experience_store"]
