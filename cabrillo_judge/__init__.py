"""Offline Cabrillo 3.0 log adjudication package.

Modules:
    parser     -- Cabrillo 3.0 text parsing and field validation
    rules      -- contest rule definitions and defaults
    engine     -- cross-log pairing, adjudication and scoring
    clockskew  -- batch-level clock-skew analysis (median/dispersion/coverage)
    storage    -- SQLite persistence
    web        -- http.server based JSON API
"""

__version__ = "1.1.0"
