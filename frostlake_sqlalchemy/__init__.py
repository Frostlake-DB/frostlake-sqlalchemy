"""SQLAlchemy dialect for Frostlake.

    from sqlalchemy import create_engine
    engine = create_engine("frostlake://localhost:18082/MY_DB?schema=PUBLIC")

The dialect rides the pure-stdlib ``frostlake`` DB-API driver over the engine's HTTP
protocol. Installing this package registers the ``frostlake://`` scheme with SQLAlchemy;
importing it registers the scheme too, for a source checkout that was never installed.
"""

from sqlalchemy.dialects import registry as _registry

from .base import FrostlakeDialect, RESERVED_WORDS, dialect
from .types import (
    ARRAY, GEOGRAPHY, GEOMETRY, NUMBER, OBJECT,
    TIMESTAMP_LTZ, TIMESTAMP_NTZ, TIMESTAMP_TZ, VARIANT,
)

__version__ = "0.1.0"

_registry.register("frostlake", "frostlake_sqlalchemy.base", "FrostlakeDialect")
_registry.register("frostlake.frostlake", "frostlake_sqlalchemy.base", "FrostlakeDialect")

__all__ = [
    "FrostlakeDialect", "RESERVED_WORDS", "dialect",
    "ARRAY", "GEOGRAPHY", "GEOMETRY", "NUMBER", "OBJECT",
    "TIMESTAMP_LTZ", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "VARIANT",
]
