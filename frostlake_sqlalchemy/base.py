"""The Frostlake dialect for SQLAlchemy.

Frostlake speaks Snowflake SQL, so this dialect follows Snowflake's conventions:
unquoted identifiers fold to upper case, the namespace is three levels deep
(database.schema.object), there are no indexes and no savepoints, and DML does not
return generated keys.

Everything the dialect emits or reflects was checked against a running engine; where the
engine differs from ANSI SQL (``OFFSET`` needs a ``LIMIT``, a ``VALUES`` clause takes no
function calls) the difference is handled here and noted in the README.
"""

import re

from sqlalchemy import exc
from sqlalchemy import schema as sa_schema
from sqlalchemy import types as sqltypes
from sqlalchemy import util
from sqlalchemy.engine import default
from sqlalchemy.engine import reflection
from sqlalchemy.sql import compiler
from sqlalchemy.sql import text
from sqlalchemy.types import (
    BIGINT, BINARY, BOOLEAN, CHAR, DATE, FLOAT, INTEGER,
    REAL, SMALLINT, TIME, VARBINARY, VARCHAR,
)

from .types import (
    ARRAY, GEOGRAPHY, GEOMETRY, NUMBER, OBJECT,
    TIMESTAMP_LTZ, TIMESTAMP_NTZ, TIMESTAMP_TZ, VARIANT,
)

# Determined by asking a live engine which words it rejects as a column name, a table
# name and a SELECT alias -- the three positions SQLAlchemy quotes identifiers for.
# Kept lower case: IdentifierPreparer folds before it looks a word up here.
RESERVED_WORDS = frozenset("""
all alter and any as between by check column connect constraint create current
current_date current_time current_timestamp current_user delete distinct drop else exists
following for from grant group having ilike in increment insert intersect into is like
minus not null of on or order qualify regexp revoke rlike row rows sample select set some
start table tablesample then to union unique update values where with
""".split())

# INFORMATION_SCHEMA.COLUMNS.DATA_TYPE -> the type to reflect it as. The engine reports
# storage types, so the spellings it accepts in DDL (STRING, INT, DOUBLE, ...) collapse
# onto the handful of names below.
ischema_names = {
    "TEXT": VARCHAR,
    "VARCHAR": VARCHAR,
    "CHAR": CHAR,
    "STRING": VARCHAR,
    "NUMBER": NUMBER,
    "DECIMAL": NUMBER,
    "NUMERIC": NUMBER,
    "FIXED": NUMBER,
    "INT": INTEGER,
    "INTEGER": INTEGER,
    "BIGINT": BIGINT,
    "SMALLINT": SMALLINT,
    "FLOAT": FLOAT,
    "DOUBLE": FLOAT,
    "REAL": REAL,
    "BOOLEAN": BOOLEAN,
    "DATE": DATE,
    "TIME": TIME,
    "DATETIME": TIMESTAMP_NTZ,
    "TIMESTAMP": TIMESTAMP_NTZ,
    "TIMESTAMP_NTZ": TIMESTAMP_NTZ,
    "TIMESTAMP_LTZ": TIMESTAMP_LTZ,
    "TIMESTAMP_TZ": TIMESTAMP_TZ,
    "BINARY": BINARY,
    "VARBINARY": VARBINARY,
    "VARIANT": VARIANT,
    "OBJECT": OBJECT,
    "ARRAY": ARRAY,
    "GEOGRAPHY": GEOGRAPHY,
    "GEOMETRY": GEOMETRY,
}


class _FrostlakeJSONIndexType(sqltypes.JSON.JSONIndexType):
    """A single ``VARIANT['key']`` / ``VARIANT[0]`` step, passed to ``GET()`` as-is."""


class _FrostlakeJSONPathType(sqltypes.JSON.JSONPathType):
    """A multi-step path, rendered as the dotted string ``GET_PATH()`` expects."""

    def bind_processor(self, dialect):
        def process(value):
            path = ""
            for element in value:
                if isinstance(element, int):
                    path += "[%d]" % element
                else:
                    path += ("." + element) if path else str(element)
            return path
        return process


class FrostlakeTypeCompiler(compiler.GenericTypeCompiler):
    def visit_NUMBER(self, type_, **kw):
        return self._number("NUMBER", type_)

    def visit_NUMERIC(self, type_, **kw):
        return self._number("NUMBER", type_)

    def visit_DECIMAL(self, type_, **kw):
        return self._number("NUMBER", type_)

    def _number(self, name, type_):
        if type_.precision is None:
            return name
        if type_.scale is None:
            return "%s(%d)" % (name, type_.precision)
        return "%s(%d, %d)" % (name, type_.precision, type_.scale)

    # The engine keeps one binary floating-point type; DOUBLE and REAL are spellings of it.
    def visit_DOUBLE(self, type_, **kw):
        return "DOUBLE"

    def visit_DOUBLE_PRECISION(self, type_, **kw):
        return "DOUBLE PRECISION"

    def visit_FLOAT(self, type_, **kw):
        return "FLOAT"

    def visit_REAL(self, type_, **kw):
        return "REAL"

    # No large-object types: a VARCHAR already holds up to 16 MB.
    def visit_TEXT(self, type_, **kw):
        return "VARCHAR"

    def visit_CLOB(self, type_, **kw):
        return "VARCHAR"

    def visit_NCLOB(self, type_, **kw):
        return "VARCHAR"

    def visit_BLOB(self, type_, **kw):
        return "BINARY"

    def visit_large_binary(self, type_, **kw):
        return "BINARY"

    def visit_NVARCHAR(self, type_, **kw):
        return self.visit_VARCHAR(type_, **kw)

    def visit_NCHAR(self, type_, **kw):
        return self.visit_CHAR(type_, **kw)

    def visit_unicode(self, type_, **kw):
        return self.visit_VARCHAR(type_, **kw)

    def visit_unicode_text(self, type_, **kw):
        return "VARCHAR"

    def visit_DATETIME(self, type_, **kw):
        return "TIMESTAMP_TZ" if getattr(type_, "timezone", False) else "TIMESTAMP_NTZ"

    def visit_TIMESTAMP(self, type_, **kw):
        return self.visit_DATETIME(type_, **kw)

    def visit_TIMESTAMP_NTZ(self, type_, **kw):
        return self._with_precision("TIMESTAMP_NTZ", type_)

    def visit_TIMESTAMP_LTZ(self, type_, **kw):
        return self._with_precision("TIMESTAMP_LTZ", type_)

    def visit_TIMESTAMP_TZ(self, type_, **kw):
        return self._with_precision("TIMESTAMP_TZ", type_)

    def visit_TIME(self, type_, **kw):
        return self._with_precision("TIME", type_)

    def _with_precision(self, name, type_):
        precision = getattr(type_, "precision", None)
        return name if precision is None else "%s(%d)" % (name, precision)

    def visit_JSON(self, type_, **kw):
        return "VARIANT"

    def visit_VARIANT(self, type_, **kw):
        return "VARIANT"

    def visit_OBJECT(self, type_, **kw):
        return "OBJECT"

    def visit_ARRAY(self, type_, **kw):
        return "ARRAY"

    def visit_GEOGRAPHY(self, type_, **kw):
        return "GEOGRAPHY"

    def visit_GEOMETRY(self, type_, **kw):
        return "GEOMETRY"


class FrostlakeCompiler(compiler.SQLCompiler):
    def limit_clause(self, select, **kw):
        """``LIMIT``/``OFFSET``, with the engine's requirement that an OFFSET be preceded
        by a LIMIT -- ``LIMIT NULL`` is the unbounded one."""
        text_ = ""
        if select._limit_clause is not None:
            text_ += "\n LIMIT " + self.process(select._limit_clause, **kw)
        if select._offset_clause is not None:
            if select._limit_clause is None:
                text_ += "\n LIMIT NULL"
            text_ += " OFFSET " + self.process(select._offset_clause, **kw)
        return text_

    def for_update_clause(self, select, **kw):
        """The engine has no row locking; a ``with_for_update()`` is dropped rather than
        sent as syntax it would reject."""
        return ""

    def visit_sequence(self, seq, **kw):
        return self.preparer.format_sequence(seq) + ".NEXTVAL"

    def visit_char_length_func(self, fn, **kw):
        return "LENGTH" + self.function_argspec(fn, **kw)

    def visit_regexp_match_op_binary(self, binary, operator, **kw):
        return "%s RLIKE %s" % (self.process(binary.left, **kw),
                                self.process(binary.right, **kw))

    def visit_not_regexp_match_op_binary(self, binary, operator, **kw):
        return "NOT (%s RLIKE %s)" % (self.process(binary.left, **kw),
                                      self.process(binary.right, **kw))

    def visit_regexp_replace_op_binary(self, binary, operator, **kw):
        return "REGEXP_REPLACE(%s, %s)" % (self.process(binary.left, **kw),
                                           self.process(binary.right, **kw))

    def visit_json_getitem_op_binary(self, binary, operator, **kw):
        return "GET(%s, %s)" % (self.process(binary.left, **kw),
                                self.process(binary.right, **kw))

    def visit_json_path_getitem_op_binary(self, binary, operator, **kw):
        return "GET_PATH(%s, %s)" % (self.process(binary.left, **kw),
                                     self.process(binary.right, **kw))

    def visit_empty_set_expr(self, element_types, **kw):
        return "SELECT %s FROM (SELECT 1) WHERE 1 != 1" % (
            ", ".join("1" for _ in element_types) or "1",
        )

    def render_literal_value(self, value, type_):
        text_ = super().render_literal_value(value, type_)
        if isinstance(value, str) and "\\" in text_:
            # The engine reads a backslash inside a string literal as an escape, so a
            # literal one has to be doubled -- the rule the driver applies to bound
            # parameters, applied here to inlined ones.
            text_ = text_.replace("\\", "\\\\")
        return text_


class FrostlakeDDLCompiler(compiler.DDLCompiler):
    def get_column_specification(self, column, **kwargs):
        colspec = [
            self.preparer.format_column(column),
            self.dialect.type_compiler_instance.process(
                column.type, type_expression=column
            ),
        ]

        default_ = self.get_column_default_string(column)
        if column.identity is not None:
            colspec.append(self.visit_identity_column(column.identity))
        elif default_ is not None:
            colspec.append("DEFAULT " + default_)
        elif (
            column.table is not None
            and column is column.table._autoincrement_column
            and column.server_default is None
            # A Sequence default already supplies the value, and it is fetched before
            # the INSERT so the ORM learns the key; a second generator on the column
            # would only be dead weight.
            and not isinstance(column.default, sa_schema.Sequence)
        ):
            colspec.append("AUTOINCREMENT")

        if not column.nullable:
            colspec.append("NOT NULL")
        if column.comment is not None:
            colspec.append("COMMENT " + self.sql_compiler.render_literal_value(
                column.comment, sqltypes.String()))
        return " ".join(colspec)

    def visit_identity_column(self, identity, **kw):
        start = 1 if identity.start is None else identity.start
        increment = 1 if identity.increment is None else identity.increment
        return "IDENTITY(%d, %d)" % (start, increment)

    def define_constraint_deferrability(self, constraint):
        """The engine parses no DEFERRABLE clause; constraints are never deferred."""
        return ""

    def post_create_table(self, table):
        parts = []
        clusterby = table.dialect_options["frostlake"]["clusterby"]
        if clusterby:
            keys = [
                self.preparer.format_column(key)
                if isinstance(key, sa_schema.Column)
                else self.preparer.quote(key)
                for key in clusterby
            ]
            parts.append(" CLUSTER BY (%s)" % ", ".join(keys))
        if table.comment is not None:
            parts.append(" COMMENT = " + self.sql_compiler.render_literal_value(
                table.comment, sqltypes.String()))
        return "".join(parts)

    def visit_create_index(self, create, **kw):
        raise exc.CompileError(
            "Frostlake has no indexes; remove the Index/index=True from table %r. "
            "Pruning is automatic, and a clustering key can be declared with "
            "Table(..., frostlake_clusterby=[...])." % create.element.table.name
        )

    def visit_drop_index(self, drop, **kw):
        raise exc.CompileError("Frostlake has no indexes; there is nothing to drop.")

    def visit_create_sequence(self, create, **kw):
        seq = create.element
        text_ = "CREATE SEQUENCE " + self.preparer.format_sequence(seq)
        if seq.start is not None:
            text_ += " START WITH %d" % seq.start
        if seq.increment is not None:
            text_ += " INCREMENT BY %d" % seq.increment
        return text_

    def visit_set_table_comment(self, create, **kw):
        return "COMMENT ON TABLE %s IS %s" % (
            self.preparer.format_table(create.element),
            self.sql_compiler.render_literal_value(
                create.element.comment, sqltypes.String()),
        )

    def visit_drop_table_comment(self, drop, **kw):
        # The engine rejects `IS NULL`; an empty comment is how one is cleared.
        return "COMMENT ON TABLE %s IS ''" % self.preparer.format_table(drop.element)

    def visit_set_column_comment(self, create, **kw):
        return "COMMENT ON COLUMN %s IS %s" % (
            self.preparer.format_column(create.element, use_table=True),
            self.sql_compiler.render_literal_value(
                create.element.comment, sqltypes.String()),
        )

    def visit_drop_column_comment(self, drop, **kw):
        return "COMMENT ON COLUMN %s IS ''" % self.preparer.format_column(
            drop.element, use_table=True)


class FrostlakeIdentifierPreparer(compiler.IdentifierPreparer):
    reserved_words = RESERVED_WORDS


class FrostlakeExecutionContext(default.DefaultExecutionContext):
    def fire_sequence(self, seq, type_):
        return self._execute_scalar(
            "SELECT " + self.identifier_preparer.format_sequence(seq) + ".NEXTVAL",
            type_,
        )


class FrostlakeDialect(default.DefaultDialect):
    name = "frostlake"
    driver = "frostlake"
    supports_statement_cache = True

    statement_compiler = FrostlakeCompiler
    ddl_compiler = FrostlakeDDLCompiler
    type_compiler_cls = FrostlakeTypeCompiler
    preparer = FrostlakeIdentifierPreparer
    execution_ctx_cls = FrostlakeExecutionContext

    default_paramstyle = "qmark"
    max_identifier_length = 255

    # Unquoted identifiers fold to upper case, so SQLAlchemy's lower-case names are
    # denormalized on the way out and normalized on the way back in.
    requires_name_normalize = True

    supports_alter = True
    supports_comments = True
    inline_comments = True          # COMMENT '...' rides along in CREATE TABLE
    supports_native_boolean = True
    supports_native_decimal = True
    supports_native_enum = False    # no ENUM type; renders as VARCHAR
    supports_native_uuid = False    # no UUID type; renders as CHAR(32)
    supports_sequences = True
    sequences_optional = False
    preexecute_autoincrement_sequences = True
    supports_identity_columns = True
    postfetch_lastrowid = False     # no RETURNING and no last-insert-id
    insert_returning = False
    update_returning = False
    delete_returning = False
    supports_default_values = False     # INSERT ... DEFAULT VALUES is not parsed
    supports_default_metavalue = True   # ... but VALUES (DEFAULT) is
    supports_empty_insert = False
    supports_multivalues_insert = True
    supports_sane_rowcount = True
    supports_sane_multi_rowcount = False
    supports_is_distinct_from = True
    supports_savepoints = False
    div_is_floordiv = False         # 10/3 is 3.333333, not 3
    returns_native_bytes = True

    ischema_names = ischema_names

    # A JSON column is a VARIANT column; the two are the same thing here.
    colspecs = {
        sqltypes.JSON: VARIANT,
        sqltypes.JSON.JSONIndexType: _FrostlakeJSONIndexType,
        sqltypes.JSON.JSONPathType: _FrostlakeJSONPathType,
    }

    construct_arguments = [
        (sa_schema.Table, {"clusterby": None}),
    ]

    def __init__(self, json_serializer=None, json_deserializer=None, **kwargs):
        super().__init__(**kwargs)
        self._json_serializer = json_serializer
        self._json_deserializer = json_deserializer

    @classmethod
    def import_dbapi(cls):
        import frostlake
        return frostlake

    # Kept for callers still using SQLAlchemy 1.4's spelling of the same hook.
    @classmethod
    def dbapi(cls):
        return cls.import_dbapi()

    def create_connect_args(self, url):
        if url.username or url.password:
            util.warn(
                "Frostlake's protocol carries no credentials; the username and "
                "password in the URL are ignored."
            )
        opts = {
            "host": url.host or "localhost",
            "port": url.port or 18082,
            "database": url.database or None,
        }
        query = dict(url.query)
        for key in ("schema", "timeout"):
            if key in query:
                value = query[key]
                if isinstance(value, tuple):
                    value = value[0]
                opts[key] = int(value) if key == "timeout" else value
        return ([], opts)

    def _get_server_version_info(self, connection):
        version = connection.exec_driver_sql("SELECT CURRENT_VERSION()").scalar()
        parts = []
        for piece in re.split(r"[.\-]", str(version or "")):
            parts.append(int(piece) if piece.isdigit() else piece)
        return tuple(part for part in parts if part != "") or None

    def _get_default_schema_name(self, connection):
        return self.normalize_name(
            connection.exec_driver_sql("SELECT CURRENT_SCHEMA()").scalar()
        )

    def _get_default_database_name(self, connection):
        return self.normalize_name(
            connection.exec_driver_sql("SELECT CURRENT_DATABASE()").scalar()
        )

    def on_connect(self):
        def connect(dbapi_connection):
            # The driver opens in autocommit; SQLAlchemy expects to own the
            # transaction, so hand control back to it.
            dbapi_connection.autocommit = False
        return connect

    def get_isolation_level(self, dbapi_connection):
        return "AUTOCOMMIT" if dbapi_connection.autocommit else "READ COMMITTED"

    def get_default_isolation_level(self, dbapi_connection):
        return "READ COMMITTED"

    def set_isolation_level(self, dbapi_connection, level):
        if level == "AUTOCOMMIT":
            dbapi_connection.autocommit = True
        elif level in ("READ COMMITTED", "READ_COMMITTED"):
            dbapi_connection.autocommit = False
        else:
            raise exc.ArgumentError(
                "Frostlake supports READ COMMITTED and AUTOCOMMIT, not %r" % level
            )

    def get_isolation_level_values(self, dbapi_connection):
        return ["READ COMMITTED", "AUTOCOMMIT"]

    def do_savepoint(self, connection, name):
        raise NotImplementedError("Frostlake has no savepoints")

    def do_rollback_to_savepoint(self, connection, name):
        raise NotImplementedError("Frostlake has no savepoints")

    def do_release_savepoint(self, connection, name):
        raise NotImplementedError("Frostlake has no savepoints")

    def is_disconnect(self, e, connection, cursor):
        if isinstance(e, self.loaded_dbapi.InterfaceError):
            return "closed" in str(e)
        # OperationalError is what the driver raises for transport failures.
        return isinstance(e, self.loaded_dbapi.OperationalError)

    # -- name plumbing ------------------------------------------------------

    def _split_schema(self, schema):
        """``"db.schema"`` -> ``("db", "schema")``; a bare name -> ``(None, name)``.

        Frostlake's namespace has three levels but SQLAlchemy's ``schema=`` argument is
        one string, so a dotted value addresses a schema in another database. Quoted
        parts keep their case; bare ones fold the way the engine folds them.
        """
        if schema is None:
            return None, None
        parts = re.findall(r'"([^"]*)"|([^.]+)', schema)
        names = [(quoted or bare, bool(quoted)) for quoted, bare in parts]
        if len(names) == 1:
            return None, self._keep_case(*names[0])
        if len(names) == 2:
            return self._keep_case(*names[0]), self._keep_case(*names[1])
        raise exc.ArgumentError(
            "schema %r has too many parts; expected 'schema' or 'database.schema'"
            % schema
        )

    def _keep_case(self, name, was_quoted):
        return name if was_quoted else self.denormalize_name(name)

    def _constraint_name(self, name):
        """A constraint's reflected name, or None when the engine made one up.

        A constraint declared without a name gets a ``SYS_CONSTRAINT_<uuid>``; handing
        that back would bake one server's random name into a reflected Table.
        """
        if name is None or str(name).upper().startswith("SYS_CONSTRAINT_"):
            return None
        return self.normalize_name(name)

    def _resolve_schema(self, connection, schema):
        """The (database, schema) a reflection call is about, in the engine's own case.
        ``database`` is None when the connection's current one applies."""
        database, schema_name = self._split_schema(schema)
        if schema_name is None:
            schema_name = self.denormalize_name(self.default_schema_name)
        return database, schema_name

    def _info_schema(self, database):
        """The INFORMATION_SCHEMA to query -- the current database's, or another's."""
        if database is None:
            return "INFORMATION_SCHEMA"
        return self.identifier_preparer.quote(database) + ".INFORMATION_SCHEMA"

    def _qualified(self, database, schema, name):
        quote = self.identifier_preparer.quote
        parts = [quote(schema), quote(name)]
        if database is not None:
            parts.insert(0, quote(database))
        return ".".join(parts)

    def _rows(self, connection, statement, **params):
        """Run a metadata query and hand back dicts keyed by lower-cased column name.

        SHOW answers with lower-case column names and INFORMATION_SCHEMA with upper-case
        ones; going through the names rather than positions keeps both readable.
        """
        result = connection.execute(text(statement), params)
        keys = [str(key).lower() for key in result.keys()]
        return [dict(zip(keys, row)) for row in result]

    def _show(self, connection, statement):
        result = connection.exec_driver_sql(statement)
        keys = [str(key).lower() for key in result.keys()]
        return [dict(zip(keys, row)) for row in result]

    # -- reflection ---------------------------------------------------------

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        return bool(self._rows(
            connection,
            "SELECT 1 FROM %s.TABLES WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        ))

    @reflection.cache
    def has_sequence(self, connection, sequence_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        return bool(self._rows(
            connection,
            "SELECT 1 FROM %s.SEQUENCES WHERE SEQUENCE_SCHEMA = :schema "
            "AND SEQUENCE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(sequence_name),
        ))

    @reflection.cache
    def has_index(self, connection, table_name, index_name, schema=None, **kw):
        return False

    @reflection.cache
    def get_schema_names(self, connection, **kw):
        database, _ = self._split_schema(kw.get("database"))
        rows = self._rows(
            connection,
            "SELECT SCHEMA_NAME FROM %s.SCHEMATA ORDER BY SCHEMA_NAME"
            % self._info_schema(database),
        )
        return [self.normalize_name(row["schema_name"]) for row in rows]

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        return self._names_of_type(connection, schema, "BASE TABLE")

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        return self._names_of_type(connection, schema, "VIEW")

    def _names_of_type(self, connection, schema, table_type):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT TABLE_NAME FROM %s.TABLES WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_TYPE = :kind ORDER BY TABLE_NAME" % self._info_schema(database),
            schema=schema_name, kind=table_type,
        )
        return [self.normalize_name(row["table_name"]) for row in rows]

    @reflection.cache
    def get_sequence_names(self, connection, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT SEQUENCE_NAME FROM %s.SEQUENCES WHERE SEQUENCE_SCHEMA = :schema "
            "ORDER BY SEQUENCE_NAME" % self._info_schema(database),
            schema=schema_name,
        )
        return [self.normalize_name(row["sequence_name"]) for row in rows]

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT VIEW_DEFINITION FROM %s.VIEWS WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(view_name),
        )
        if not rows:
            raise exc.NoSuchTableError(view_name)
        return rows[0]["view_definition"]

    @reflection.cache
    def get_table_comment(self, connection, table_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT COMMENT FROM %s.TABLES WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        )
        if not rows:
            raise exc.NoSuchTableError(table_name)
        return {"text": rows[0]["comment"] or None}

    @reflection.cache
    def get_table_options(self, connection, table_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT CLUSTERING_KEY FROM %s.TABLES WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        )
        if not rows:
            raise exc.NoSuchTableError(table_name)
        key = rows[0]["clustering_key"]
        if not key:
            return {}
        return {"frostlake_clusterby": [part.strip() for part in key.split(",")]}

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            """SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                      NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION,
                      IS_NULLABLE, COLUMN_DEFAULT, COMMENT, IS_IDENTITY
               FROM %s.COLUMNS
               WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :name
               ORDER BY ORDINAL_POSITION""" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        )
        if not rows:
            raise exc.NoSuchTableError(table_name)

        identity_options = None
        columns = []
        for row in rows:
            is_identity = str(row.get("is_identity") or "").upper() == "YES"
            column = {
                "name": self.normalize_name(row["column_name"]),
                "type": self._resolve_type(row),
                "nullable": str(row["is_nullable"]).upper() == "YES",
                "default": None if is_identity else row["column_default"],
                "autoincrement": is_identity,
                "comment": row["comment"] or None,
            }
            if is_identity:
                if identity_options is None:
                    identity_options = self._identity_options(
                        connection, database, schema_name, table_name)
                column["identity"] = identity_options.get(
                    row["column_name"], {"start": 1, "increment": 1})
            columns.append(column)
        return columns

    def _identity_options(self, connection, database, schema, table_name):
        """The seed and step of each identity column, read from DESCRIBE TABLE.

        INFORMATION_SCHEMA.COLUMNS answers 1/1 for IDENTITY_START and
        IDENTITY_INCREMENT whatever the column was declared with (engine 0.0.7), while
        DESCRIBE renders the real clause -- ``IDENTITY START 5 INCREMENT 2 NOORDER`` --
        as the column's default. Reading it there keeps a reflected table re-creatable.
        """
        rows = self._show(connection, "DESCRIBE TABLE " + self._qualified(
            database, schema, self.denormalize_name(table_name)))
        options = {}
        for row in rows:
            match = re.search(r"IDENTITY\s+START\s+(-?\d+)\s+INCREMENT\s+(-?\d+)",
                              str(row.get("default") or ""), re.IGNORECASE)
            if match:
                options[row["name"]] = {"start": int(match.group(1)),
                                        "increment": int(match.group(2))}
        return options

    def _resolve_type(self, row):
        name = str(row["data_type"] or "").upper()
        type_class = self.ischema_names.get(name)
        if type_class is None:
            util.warn("Did not recognize type %r of column %r; using NullType"
                      % (name, row["column_name"]))
            return sqltypes.NULLTYPE

        if type_class in (VARCHAR, CHAR):
            length = row["character_maximum_length"]
            return type_class(int(length)) if length else type_class()
        if type_class is NUMBER:
            precision = row["numeric_precision"]
            scale = row["numeric_scale"]
            if precision is None:
                return NUMBER()
            return NUMBER(precision=int(precision),
                          scale=None if scale is None else int(scale))
        if type_class in (TIME, TIMESTAMP_NTZ, TIMESTAMP_LTZ, TIMESTAMP_TZ):
            precision = row["datetime_precision"]
            return type_class() if precision is None else type_class(int(precision))
        return type_class()

    @reflection.cache
    def _is_base_table(self, connection, table_name, schema=None, **kw):
        """Whether the relation is a table rather than a view.

        The key constraints are read with SHOW ... IN TABLE, which errors on a view --
        so a view is answered from here instead, with the empty result it really has.
        """
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT TABLE_TYPE FROM %s.TABLES WHERE TABLE_SCHEMA = :schema "
            "AND TABLE_NAME = :name" % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        )
        return bool(rows) and rows[0]["table_type"] == "BASE TABLE"

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        if not self._is_base_table(connection, table_name, schema=schema, **kw):
            return {"constrained_columns": [], "name": None}
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._show(connection, "SHOW PRIMARY KEYS IN TABLE " + self._qualified(
            database, schema_name, self.denormalize_name(table_name)))
        rows.sort(key=lambda row: row["key_sequence"])
        return {
            "constrained_columns": [
                self.normalize_name(row["column_name"]) for row in rows
            ],
            "name": self._constraint_name(rows[0]["constraint_name"]) if rows else None,
        }

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        if not self._is_base_table(connection, table_name, schema=schema, **kw):
            return []
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._show(connection, "SHOW IMPORTED KEYS IN TABLE " + self._qualified(
            database, schema_name, self.denormalize_name(table_name)))
        current_database = self._current_database(connection)

        # One row per column; rows of the same constraint are gathered in key_sequence
        # order, so a composite key keeps the column pairing its definition had.
        keys = {}
        for row in sorted(rows, key=lambda r: r["key_sequence"]):
            name = row["fk_name"]
            entry = keys.get(name)
            if entry is None:
                referred_database = self.normalize_name(row["pk_database_name"])
                referred_schema = self.normalize_name(row["pk_schema_name"])
                if referred_database != current_database:
                    # Another database: keep it addressable by handing back the dotted
                    # schema this dialect also accepts as an argument.
                    referred_schema = "%s.%s" % (referred_database, referred_schema)
                entry = keys[name] = {
                    "name": self._constraint_name(name),
                    "constrained_columns": [],
                    "referred_schema": referred_schema,
                    "referred_table": self.normalize_name(row["pk_table_name"]),
                    "referred_columns": [],
                    "options": {},
                }
                for column, option in (("update_rule", "onupdate"),
                                       ("delete_rule", "ondelete")):
                    rule = str(row.get(column) or "").upper()
                    if rule and rule != "NO ACTION":
                        entry["options"][option] = rule
            entry["constrained_columns"].append(
                self.normalize_name(row["fk_column_name"]))
            entry["referred_columns"].append(
                self.normalize_name(row["pk_column_name"]))
        return list(keys.values())

    def _current_database(self, connection):
        return self.normalize_name(
            connection.exec_driver_sql("SELECT CURRENT_DATABASE()").scalar())

    @reflection.cache
    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        if not self._is_base_table(connection, table_name, schema=schema, **kw):
            return []
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._show(connection, "SHOW UNIQUE KEYS IN TABLE " + self._qualified(
            database, schema_name, self.denormalize_name(table_name)))
        constraints = {}
        for row in sorted(rows, key=lambda r: r["key_sequence"]):
            name = row["constraint_name"]
            entry = constraints.setdefault(
                name, {"name": self._constraint_name(name), "column_names": []})
            entry["column_names"].append(self.normalize_name(row["column_name"]))
        return list(constraints.values())

    @reflection.cache
    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        database, schema_name = self._resolve_schema(connection, schema)
        rows = self._rows(
            connection,
            "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM %s.CHECK_CONSTRAINTS "
            "WHERE CONSTRAINT_SCHEMA = :schema AND CONSTRAINT_TABLE = :name"
            % self._info_schema(database),
            schema=schema_name, name=self.denormalize_name(table_name),
        )
        return [
            {"name": self._constraint_name(row["constraint_name"]),
             "sqltext": row["check_clause"]}
            for row in rows
        ]

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kw):
        """Always empty: the engine has no indexes."""
        return []


dialect = FrostlakeDialect
