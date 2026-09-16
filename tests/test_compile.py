"""Compilation tests: what SQL the dialect emits. No server and no driver needed."""

import decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, Enum, Float,
    ForeignKey, Identity, Index, Integer, LargeBinary, MetaData, Numeric, Sequence,
    String, Table, Text, Time, Unicode, UniqueConstraint, Uuid, select,
)
from sqlalchemy.schema import (
    CreateSequence, CreateTable, DropSequence, SetColumnComment, SetTableComment,
)

from frostlake_sqlalchemy import VARIANT
from frostlake_sqlalchemy.base import FrostlakeDialect

dialect = FrostlakeDialect()


def compile_sql(element):
    return str(element.compile(dialect=dialect)).strip()


@pytest.fixture
def metadata():
    return MetaData()


@pytest.fixture
def people(metadata):
    return Table(
        "people", metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer),
    )


# -- SELECT -----------------------------------------------------------------

def literal_sql(element):
    return str(element.compile(dialect=dialect,
                               compile_kwargs={"literal_binds": True})).strip()


def test_limit_and_offset(people):
    assert "LIMIT 5" in literal_sql(select(people.c.id).limit(5))
    sql = literal_sql(select(people.c.id).limit(5).offset(2))
    assert "LIMIT 5 OFFSET 2" in sql


def test_offset_without_limit_gets_limit_null(people):
    """The engine rejects a bare OFFSET, so an unbounded LIMIT precedes it."""
    assert "LIMIT NULL OFFSET 2" in literal_sql(select(people.c.id).offset(2))


def test_for_update_is_dropped(people):
    """There is no row locking, so the clause is left off rather than sent."""
    assert "FOR UPDATE" not in compile_sql(select(people.c.id).with_for_update())


def test_char_length_is_length(people):
    assert "LENGTH(people.name)" in compile_sql(select(sa.func.char_length(people.c.name)))


def test_regexp_match_uses_rlike(people):
    sql = compile_sql(select(people.c.id).where(people.c.name.regexp_match("a.*")))
    assert "people.name RLIKE" in sql


def test_not_regexp_match(people):
    sql = compile_sql(select(people.c.id).where(~people.c.name.regexp_match("a.*")))
    assert "NOT (people.name RLIKE" in sql


def test_is_distinct_from(people):
    assert "IS DISTINCT FROM" in compile_sql(
        select(people.c.id).where(people.c.age.is_distinct_from(1)))


def test_reserved_words_are_quoted(metadata):
    table = Table("order", metadata,
                  Column("select", Integer), Column("plain", Integer))
    sql = compile_sql(select(table))
    assert '"order"' in sql and '"select"' in sql
    assert '"plain"' not in sql


def test_backslash_in_literal_is_doubled(people):
    sql = select(people.c.id).where(people.c.name == "a\\b").compile(
        dialect=dialect, compile_kwargs={"literal_binds": True})
    assert "'a\\\\b'" in str(sql)


def test_json_getitem(metadata):
    table = Table("docs", metadata, Column("payload", sa.JSON))
    assert "GET(docs.payload" in compile_sql(select(table.c.payload["k"]))


def test_json_path_getitem(metadata):
    table = Table("docs", metadata, Column("payload", sa.JSON))
    assert "GET_PATH(docs.payload" in compile_sql(
        select(table.c.payload[("a", "b")]))


def test_json_bind_is_wrapped_in_parse_json(metadata):
    table = Table("docs", metadata, Column("id", Integer), Column("payload", sa.JSON))
    assert "parse_json(" in compile_sql(table.insert())


def test_variant_bind_is_wrapped_in_parse_json(metadata):
    table = Table("docs", metadata, Column("id", Integer), Column("payload", VARIANT))
    assert "parse_json(" in compile_sql(table.insert())


def test_sequence_renders_nextval():
    assert compile_sql(select(Sequence("my_seq").next_value())) == \
        "SELECT my_seq.NEXTVAL AS next_value_1"


# -- DDL --------------------------------------------------------------------

def test_create_table_types(metadata):
    table = Table(
        "t", metadata,
        Column("a", Integer),
        Column("b", BigInteger),
        Column("c", String(10)),
        Column("d", Text),
        Column("e", Unicode(5)),
        Column("f", Numeric(12, 2)),
        Column("g", Float),
        Column("h", Boolean),
        Column("i", Date),
        Column("j", Time),
        Column("k", DateTime),
        Column("l", DateTime(timezone=True)),
        Column("m", LargeBinary),
        Column("n", VARIANT),
        Column("o", sa.JSON),
        Column("p", Enum("x", "y", name="e")),
        Column("q", Uuid),
    )
    sql = compile_sql(CreateTable(table))
    for expected in ("a INTEGER", "b BIGINT", "c VARCHAR(10)", "d VARCHAR",
                     "e VARCHAR(5)", "f NUMBER(12, 2)", "g FLOAT", "h BOOLEAN",
                     "i DATE", "j TIME", "k TIMESTAMP_NTZ", "l TIMESTAMP_TZ",
                     "m BINARY", "n VARIANT", "o VARIANT", "p VARCHAR(1)",
                     "q CHAR(32)"):
        assert expected in sql, expected


def test_autoincrement_primary_key(people):
    assert "id INTEGER AUTOINCREMENT NOT NULL" in compile_sql(CreateTable(people))


def test_sequence_default_suppresses_autoincrement(metadata):
    """A Sequence already supplies the key; a second generator would be dead weight."""
    table = Table("t", metadata,
                  Column("id", Integer, Sequence("t_seq"), primary_key=True))
    assert "AUTOINCREMENT" not in compile_sql(CreateTable(table))


def test_identity_column(metadata):
    table = Table("t", metadata,
                  Column("id", Integer, Identity(start=5, increment=2),
                         primary_key=True))
    assert "IDENTITY(5, 2)" in compile_sql(CreateTable(table))


def test_comments_are_inline(metadata):
    table = Table("t", metadata,
                  Column("a", Integer, comment="col comment"),
                  comment="table comment")
    sql = compile_sql(CreateTable(table))
    assert "a INTEGER COMMENT 'col comment'" in sql
    assert "COMMENT = 'table comment'" in sql


def test_cluster_by(metadata):
    table = Table("t", metadata, Column("a", Integer), Column("b", Integer),
                  frostlake_clusterby=["a", "b"])
    assert "CLUSTER BY (a, b)" in compile_sql(CreateTable(table))


def test_constraints(metadata):
    parent = Table("parent", metadata, Column("id", Integer, primary_key=True))
    child = Table(
        "child", metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parent.id", ondelete="CASCADE")),
        Column("code", String(5)),
        UniqueConstraint("code", name="uq_child_code"),
        CheckConstraint("id > 0", name="ck_child_id"),
    )
    assert parent is not None
    sql = compile_sql(CreateTable(child))
    assert "FOREIGN KEY(parent_id) REFERENCES parent (id) ON DELETE CASCADE" in sql
    assert "CONSTRAINT uq_child_code UNIQUE (code)" in sql
    assert "CONSTRAINT ck_child_id CHECK (id > 0)" in sql


def test_deferrable_is_not_emitted(metadata):
    """The engine parses no DEFERRABLE clause, so it is left off."""
    parent = Table("parent", metadata, Column("id", Integer, primary_key=True))
    child = Table("child", metadata,
                  Column("parent_id", Integer,
                         ForeignKey("parent.id", deferrable=True,
                                    initially="DEFERRED")))
    assert parent is not None
    assert "DEFERRABLE" not in compile_sql(CreateTable(child))


def test_index_is_refused(metadata):
    table = Table("t", metadata, Column("a", Integer))
    index = Index("ix_t_a", table.c.a)
    with pytest.raises(sa.exc.CompileError, match="no indexes"):
        compile_sql(sa.schema.CreateIndex(index))


def test_sequence_ddl():
    seq = Sequence("s", start=5, increment=2)
    assert compile_sql(CreateSequence(seq)) == \
        "CREATE SEQUENCE s START WITH 5 INCREMENT BY 2"
    assert compile_sql(DropSequence(seq)) == "DROP SEQUENCE s"


def test_comment_ddl(metadata):
    table = Table("t", metadata, Column("a", Integer, comment="c"), comment="t")
    assert compile_sql(SetTableComment(table)) == "COMMENT ON TABLE t IS 't'"
    assert compile_sql(SetColumnComment(table.c.a)) == "COMMENT ON COLUMN t.a IS 'c'"


def test_temporary_table(metadata):
    table = Table("t", metadata, Column("a", Integer), prefixes=["TEMPORARY"])
    assert compile_sql(CreateTable(table)).startswith("CREATE TEMPORARY TABLE t")


# -- dialect surface --------------------------------------------------------

def test_connect_args_defaults():
    url = sa.engine.url.make_url("frostlake://")
    _, opts = dialect.create_connect_args(url)
    assert opts == {"host": "localhost", "port": 18082, "database": None}


def test_connect_args_full():
    url = sa.engine.url.make_url("frostlake://h:1234/DB?schema=S&timeout=30")
    _, opts = dialect.create_connect_args(url)
    assert opts == {"host": "h", "port": 1234, "database": "DB",
                    "schema": "S", "timeout": 30}


def test_credentials_in_url_warn():
    url = sa.engine.url.make_url("frostlake://user:pw@h:1/DB")
    with pytest.warns(sa.exc.SAWarning, match="no credentials"):
        dialect.create_connect_args(url)


def test_name_normalization():
    assert dialect.normalize_name("PEOPLE") == "people"
    assert dialect.denormalize_name("people") == "PEOPLE"
    assert dialect.normalize_name("MixedCase") == "MixedCase"


@pytest.mark.parametrize("schema,expected", [
    ("sc", (None, "SC")),
    ("db.sc", ("DB", "SC")),
    ('"Db"."Sc"', ("Db", "Sc")),
    (None, (None, None)),
])
def test_split_schema(schema, expected):
    assert dialect._split_schema(schema) == expected


def test_split_schema_rejects_three_parts():
    with pytest.raises(sa.exc.ArgumentError, match="too many parts"):
        dialect._split_schema("a.b.c")


def test_system_constraint_names_are_suppressed():
    assert dialect._constraint_name("SYS_CONSTRAINT_1234") is None
    assert dialect._constraint_name("UQ_CODE") == "uq_code"


def test_savepoints_are_refused():
    with pytest.raises(NotImplementedError):
        dialect.do_savepoint(None, "sp")


def test_decimal_stays_exact(people):
    """A Numeric bind keeps its digits rather than becoming a float."""
    sql = select(people.c.id).where(
        people.c.age == decimal.Decimal("1.10")).compile(
        dialect=dialect, compile_kwargs={"literal_binds": True})
    assert "1.10" in str(sql)
