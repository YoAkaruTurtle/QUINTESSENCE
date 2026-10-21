"""PCS2-007 domain layer: minimal PersonaProfile, Episode and current search.

Layering: the domain layer builds on ``quintessence_pcs.store`` and never writes SQL
of its own.  ``PersonaProfileService`` owns the Profile lifecycle;
``EpisodeService`` builds Episodes and searches current versions.

The underlying transaction and deletion semantics are the frozen PCS2-006 ones
(cross-card constraint 14).  This package only consumes them.
"""

from __future__ import annotations

from .errors import HistoricalSearchUnsupported, SourceUntrusted
from .memory import (
    EpisodeHit,
    EpisodeService,
    EpisodeShape,
    SearchOmission,
    SearchResult,
    search_episodes,
)
from .persona import (
    PERSONA_PROFILE_KIND,
    ActiveProfileState,
    PersonaProfileService,
)
from .refs import EventRef, RecordRef
from .scope import SCOPE_FIELDS, normalize_scope, scope_matches

__all__ = [
    "ActiveProfileState",
    "EpisodeHit",
    "EpisodeService",
    "EpisodeShape",
    "EventRef",
    "HistoricalSearchUnsupported",
    "PERSONA_PROFILE_KIND",
    "PersonaProfileService",
    "RecordRef",
    "SCOPE_FIELDS",
    "SearchOmission",
    "SearchResult",
    "SourceUntrusted",
    "normalize_scope",
    "scope_matches",
    "search_episodes",
]
