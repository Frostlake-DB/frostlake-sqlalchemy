"""Frostlake-specific column types.

The semi-structured trio (:class:`VARIANT`, :class:`OBJECT`, :class:`ARRAY`) travels as
JSON: values are serialized on the way in and wrapped in ``PARSE_JSON()``, and parsed back
into Python objects on the way out.

Because the engine rejects function calls inside a ``VALUES`` clause, a semi-structured
column can only be written through ``INSERT ... SELECT`` -- see the README's
"Semi-structured data" section.
"""

from sqlalchemy import func
from sqlalchemy import types as sqltypes

__all__ = [
    "VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY",
    "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "NUMBER",
]


class _SemiStructured(sqltypes.JSON):
    """Shared behaviour of VARIANT/OBJECT/ARRAY.

    Built on :class:`sqlalchemy.JSON`, so a column of one of these types serializes and
    deserializes documents and supports ``col["key"]`` indexing, the same as JSON does
    everywhere else. All that is added is the ``PARSE_JSON()`` the engine wants around
    the value it is given.
    """

    def bind_expression(self, bindvalue):
        return func.parse_json(bindvalue, type_=self)


class VARIANT(_SemiStructured):
    """A VARIANT column: any JSON document, or a JSON-encodable Python value."""

    __visit_name__ = "VARIANT"


class OBJECT(_SemiStructured):
    """An OBJECT column: reads back as a ``dict``."""

    __visit_name__ = "OBJECT"


class ARRAY(_SemiStructured):
    """An ARRAY column: reads back as a ``list``.

    Note this shadows :class:`sqlalchemy.ARRAY`, which models a different thing
    (a typed, dimensioned SQL array); Frostlake's ARRAY is untyped and semi-structured.
    """

    __visit_name__ = "ARRAY"


class GEOGRAPHY(sqltypes.TypeEngine):
    """A GEOGRAPHY column. Values come back in the session's rendering (GeoJSON by
    default) as text; no client-side parsing is applied."""

    __visit_name__ = "GEOGRAPHY"


class GEOMETRY(sqltypes.TypeEngine):
    """A GEOMETRY column, handled as text like :class:`GEOGRAPHY`."""

    __visit_name__ = "GEOMETRY"


class TIMESTAMP_NTZ(sqltypes.TIMESTAMP):
    """TIMESTAMP_NTZ -- a wall-clock timestamp with no zone. What plain
    :class:`~sqlalchemy.DateTime` maps to."""

    __visit_name__ = "TIMESTAMP_NTZ"

    def __init__(self, precision=None):
        super().__init__(timezone=False)
        self.precision = precision


class TIMESTAMP_LTZ(sqltypes.TIMESTAMP):
    """TIMESTAMP_LTZ -- an instant, rendered in the session time zone."""

    __visit_name__ = "TIMESTAMP_LTZ"

    def __init__(self, precision=None):
        super().__init__(timezone=True)
        self.precision = precision


class TIMESTAMP_TZ(sqltypes.TIMESTAMP):
    """TIMESTAMP_TZ -- an instant that carries its own offset. What
    ``DateTime(timezone=True)`` maps to."""

    __visit_name__ = "TIMESTAMP_TZ"

    def __init__(self, precision=None):
        super().__init__(timezone=True)
        self.precision = precision


class NUMBER(sqltypes.Numeric):
    """The engine's own spelling of a fixed-point number; ``NUMBER(38, 0)`` by default.

    Equivalent to :class:`~sqlalchemy.Numeric`, and what reflection hands back.
    """

    __visit_name__ = "NUMBER"
