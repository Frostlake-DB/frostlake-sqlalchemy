"""Integration tests against a running Frostlake engine.

Booted by conftest from FROSTLAKE_CLASSPATH; the whole module skips without it.
"""

import datetime
import decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Identity, Integer,
    LargeBinary, Numeric, Sequence, String, Table, Time, UniqueConstraint, func,
    insert, inspect, select,
)
from sqlalchemy.orm import DeclarativeBase, Session

from frostlake_sqlalchemy import (
    ARRAY, GEOGRAPHY, OBJECT, TIMESTAMP_LTZ, TIMESTAMP_NTZ, VARIANT,
)

from .conftest import TEST_SCHEMA


@pytest.fixture
def people(metadata, engine):
    table = Table(
        "people", metadata,
        Column("id", Integer, Sequence("people_seq"), primary_key=True),
        Column("name", String(50), nullable=False),
        Column("balance", Numeric(12, 2)),
        Column("active", Boolean),
        Column("born", Date),
        Column("seen", DateTime),
        Column("start", Time),
        Column("score", Float),
        Column("payload", LargeBinary),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(table), [
            {"id": 1, "name": "ada", "balance": decimal.Decimal("10.50"),
             "active": True, "born": datetime.date(1815, 12, 10),
             "seen": datetime.datetime(2020, 1, 2, 3, 4, 5),
             "start": datetime.time(9, 30), "score": 1.5, "payload": b"\x01\x02"},
            {"id": 2, "name": "alan", "balance": decimal.Decimal("3.25"),
             "active": False, "born": datetime.date(1912, 6, 23),
             "seen": None, "start": None, "score": None, "payload": None},
        ])
    return table


# -- round trips ------------------------------------------------------------

def test_types_round_trip(connection, people):
    row = connection.execute(
        select(people).where(people.c.name == "ada")).one()
    assert row.id == 1
    assert row.name == "ada"
    assert row.balance == decimal.Decimal("10.50")
    assert row.active is True
    assert row.born == datetime.date(1815, 12, 10)
    assert row.seen == datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert row.start == datetime.time(9, 30)
    assert row.score == 1.5
    assert row.payload == b"\x01\x02"


def test_nulls_round_trip(connection, people):
    row = connection.execute(select(people).where(people.c.name == "alan")).one()
    assert (row.seen, row.start, row.score, row.payload) == (None, None, None, None)


def test_limit_offset(connection, people):
    ordered = select(people.c.name).order_by(people.c.name)
    assert connection.execute(ordered.limit(1)).scalars().all() == ["ada"]
    assert connection.execute(ordered.offset(1)).scalars().all() == ["alan"]
    assert connection.execute(ordered.limit(1).offset(1)).scalars().all() == ["alan"]


def test_fetch_clause(connection, people):
    """The ANSI OFFSET ... FETCH FIRST form the engine also accepts."""
    rows = connection.execute(
        select(people.c.name).order_by(people.c.name).offset(1).fetch(1)
    ).scalars().all()
    assert rows == ["alan"]


def test_in_and_empty_in(connection, people):
    assert connection.execute(
        select(people.c.name).where(people.c.name.in_(["ada", "nobody"]))
    ).scalars().all() == ["ada"]
    assert connection.execute(
        select(people.c.name).where(people.c.name.in_([]))).scalars().all() == []


def test_like_and_ilike(connection, people):
    assert len(connection.execute(
        select(people.c.name).where(people.c.name.like("a%"))).all()) == 2
    assert len(connection.execute(
        select(people.c.name).where(people.c.name.ilike("A%"))).all()) == 2


def test_regexp_match_is_whole_string(connection, people):
    """RLIKE anchors the pattern to the whole value, as on Snowflake."""
    assert connection.execute(
        select(people.c.name).where(people.c.name.regexp_match("a.a"))
    ).scalars().all() == ["ada"]
    assert connection.execute(
        select(people.c.name).where(people.c.name.regexp_match("a"))
    ).scalars().all() == []


def test_functions(connection, people):
    assert connection.execute(
        select(func.count()).select_from(people)).scalar() == 2
    assert connection.execute(
        select(func.char_length(people.c.name)).where(
            people.c.name == "alan")).scalar() == 4
    assert connection.execute(select(func.current_date())).scalar() is not None


def test_division_is_not_floor(connection):
    """10/3 keeps its fraction; the dialect does not pretend to floor-divide."""
    assert connection.execute(select(sa.literal(10) / 3)).scalar() > 3


def test_rowcount(connection, people):
    result = connection.execute(
        people.update().where(people.c.name == "alan").values(score=9.0))
    assert result.rowcount == 1
    result = connection.execute(people.delete().where(people.c.name == "alan"))
    assert result.rowcount == 1
    connection.rollback()


def test_executemany(connection, people):
    connection.execute(insert(people), [
        {"id": 10, "name": "grace"}, {"id": 11, "name": "edsger"},
    ])
    assert connection.execute(
        select(func.count()).select_from(people)).scalar() == 4
    connection.rollback()


def test_multivalues_insert(connection, people):
    connection.execute(insert(people).values(
        [{"id": 20, "name": "a"}, {"id": 21, "name": "b"}]))
    assert connection.execute(select(func.count()).select_from(people)).scalar() == 4
    connection.rollback()


def test_server_defaults_apply(metadata, engine):
    table = Table("defaulted", metadata,
                  Column("a", Integer, server_default=sa.text("7")),
                  Column("b", String(5), server_default=sa.text("'x'")))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(b="y"))
        assert conn.execute(select(table)).one() == (7, "y")


def test_insert_with_no_values_at_all_is_unsupported(metadata, engine):
    """There is no DEFAULT VALUES clause and no `() VALUES ()`, so a row made purely
    of defaults has to name at least one column. Same limitation as Oracle."""
    table = Table("all_default", metadata,
                  Column("a", Integer, server_default=sa.text("7")))
    metadata.create_all(engine)
    with engine.begin() as conn:
        with pytest.raises(sa.exc.DatabaseError):
            conn.execute(table.insert())


def test_transaction_rollback(engine, people):
    with engine.connect() as conn:
        conn.execute(insert(people).values(id=99, name="temp"))
        assert conn.execute(select(func.count()).select_from(people)).scalar() == 3
        conn.rollback()
        assert conn.execute(select(func.count()).select_from(people)).scalar() == 2


def test_transaction_commit(engine, people):
    with engine.begin() as conn:
        conn.execute(insert(people).values(id=98, name="kept"))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(people)).scalar() == 3
        conn.execute(people.delete().where(people.c.id == 98))
        conn.commit()


def test_autocommit_isolation_level(engine, people):
    with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT") as conn:
        conn.execute(insert(people).values(id=97, name="auto"))
        conn.rollback()  # nothing to undo: the insert already committed
    with engine.connect() as conn:
        assert conn.execute(
            select(people.c.name).where(people.c.id == 97)).scalar() == "auto"
        conn.execute(people.delete().where(people.c.id == 97))
        conn.commit()


def test_quoted_mixed_case_identifiers(metadata, engine):
    table = Table("MixedCase", metadata,
                  Column("Id", Integer), Column("plain", Integer))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values({"Id": 1, "plain": 2}))
        assert conn.execute(select(table.c.Id)).scalar() == 1
    assert "MixedCase" in inspect(engine).get_table_names()


def test_reserved_word_identifiers(metadata, engine):
    table = Table("order", metadata, Column("select", Integer))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values({"select": 7}))
        assert conn.execute(select(table.c.select)).scalar() == 7


# -- semi-structured --------------------------------------------------------

@pytest.fixture
def docs(metadata, engine):
    table = Table(
        "docs", metadata,
        Column("id", Integer, primary_key=True),
        Column("payload", VARIANT),
        Column("meta", OBJECT),
        Column("plain", sa.JSON),
    )
    metadata.create_all(engine)
    return table


def test_variant_round_trip(engine, docs):
    """A VARIANT is written through INSERT ... SELECT: the engine takes no function
    call inside a VALUES clause."""
    with engine.begin() as conn:
        conn.execute(docs.insert().from_select(
            ["id", "payload", "meta", "plain"],
            select(sa.literal(1),
                   sa.literal({"k": [1, 2]}, VARIANT),
                   sa.literal({"a": "b"}, OBJECT),
                   sa.literal({"n": 1}, sa.JSON))))
        row = conn.execute(select(docs)).one()
    assert row.payload == {"k": [1, 2]}
    assert row.meta == {"a": "b"}
    assert row.plain == {"n": 1}


def test_variant_in_values_is_rejected(engine, docs):
    """Documented limitation, pinned so a future engine change is noticed."""
    with engine.begin() as conn:
        with pytest.raises(sa.exc.DatabaseError):
            conn.execute(docs.insert().values(id=2, payload={"k": 1}))


def test_json_getitem(engine, docs):
    with engine.begin() as conn:
        conn.execute(docs.insert().from_select(
            ["id", "payload"],
            select(sa.literal(3), sa.literal({"k": {"n": 5}}, VARIANT))))
        value = conn.execute(
            select(docs.c.payload["k"]).where(docs.c.id == 3)).scalar()
        assert value == {"n": 5}


# -- reflection -------------------------------------------------------------

def test_get_table_names_and_views(engine, people):
    insp = inspect(engine)
    assert "people" in insp.get_table_names()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE OR REPLACE VIEW v_people AS SELECT id FROM people")
    insp = inspect(engine)
    assert "v_people" in insp.get_view_names()
    assert "v_people" not in insp.get_table_names()
    assert "SELECT id FROM people" in insp.get_view_definition("v_people")
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP VIEW v_people")


def test_get_columns(engine, people):
    columns = {c["name"]: c for c in inspect(engine).get_columns("people")}
    assert set(columns) == {"id", "name", "balance", "active", "born", "seen",
                            "start", "score", "payload"}
    assert isinstance(columns["name"]["type"], sa.VARCHAR)
    assert columns["name"]["type"].length == 50
    assert columns["name"]["nullable"] is False
    assert columns["balance"]["type"].precision == 12
    assert columns["balance"]["type"].scale == 2
    assert isinstance(columns["active"]["type"], sa.BOOLEAN)
    assert isinstance(columns["born"]["type"], sa.DATE)
    assert isinstance(columns["score"]["type"], sa.FLOAT)
    assert isinstance(columns["payload"]["type"], sa.BINARY)


def test_get_columns_of_missing_table(engine):
    with pytest.raises(sa.exc.NoSuchTableError):
        inspect(engine).get_columns("no_such_table")


def test_reflect_semi_structured(engine, docs):
    columns = {c["name"]: c for c in inspect(engine).get_columns("docs")}
    assert isinstance(columns["payload"]["type"], VARIANT)
    assert isinstance(columns["meta"]["type"], OBJECT)


def test_identity_reflection(metadata, engine):
    table = Table("ident", metadata,
                  Column("id", Integer, Identity(start=5, increment=2),
                         primary_key=True),
                  Column("n", String(5)))
    assert table is not None
    metadata.create_all(engine)
    column = inspect(engine).get_columns("ident")[0]
    assert column["autoincrement"] is True
    assert column["identity"] == {"start": 5, "increment": 2}


def test_constraint_reflection(metadata, engine):
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata,
          Column("id", Integer, primary_key=True),
          Column("parent_id", Integer, ForeignKey("parent.id")),
          Column("code", String(5)),
          UniqueConstraint("code", name="uq_child_code"))
    metadata.create_all(engine)
    insp = inspect(engine)

    assert insp.get_pk_constraint("child")["constrained_columns"] == ["id"]
    # The engine names an undeclared constraint itself; that name is not reported.
    assert insp.get_pk_constraint("child")["name"] is None

    keys = insp.get_foreign_keys("child")
    assert len(keys) == 1
    assert keys[0]["constrained_columns"] == ["parent_id"]
    assert keys[0]["referred_table"] == "parent"
    assert keys[0]["referred_columns"] == ["id"]
    assert keys[0]["referred_schema"] == TEST_SCHEMA

    unique = insp.get_unique_constraints("child")
    assert unique == [{"name": "uq_child_code", "column_names": ["code"]}]

    assert insp.get_indexes("child") == []
    assert insp.has_index("child", "anything") is False


def test_composite_key_reflection(metadata, engine):
    Table("comp", metadata,
          Column("a", Integer, primary_key=True),
          Column("b", Integer, primary_key=True))
    metadata.create_all(engine)
    assert inspect(engine).get_pk_constraint("comp")["constrained_columns"] == \
        ["a", "b"]


def test_comment_reflection(metadata, engine):
    Table("commented", metadata,
          Column("a", Integer, comment="col note"), comment="table note")
    metadata.create_all(engine)
    insp = inspect(engine)
    assert insp.get_table_comment("commented") == {"text": "table note"}
    assert insp.get_columns("commented")[0]["comment"] == "col note"


def test_cluster_by_reflection(metadata, engine):
    Table("clustered", metadata, Column("a", Integer), Column("b", Integer),
          frostlake_clusterby=["a"])
    metadata.create_all(engine)
    options = inspect(engine).get_table_options("clustered")
    assert options == {"frostlake_clusterby": ["a"]}


def test_has_table_and_schemas(engine, people):
    insp = inspect(engine)
    assert insp.has_table("people") is True
    assert insp.has_table("nothing_here") is False
    assert TEST_SCHEMA in insp.get_schema_names()


def test_sequence_reflection(engine, people):
    insp = inspect(engine)
    assert "people_seq" in insp.get_sequence_names()
    assert insp.has_sequence("people_seq") is True
    assert insp.has_sequence("no_such_seq") is False


def test_autoload_round_trip(metadata, engine, people):
    reflected = Table("people", sa.MetaData(), autoload_with=engine)
    assert [c.name for c in reflected.columns] == \
        [c.name for c in people.columns]
    assert reflected.c.name.type.length == 50
    assert reflected.c.name.nullable is False


def test_autoload_view(engine, people):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE OR REPLACE VIEW v_names AS SELECT id, name FROM people")
    try:
        view = Table("v_names", sa.MetaData(), autoload_with=engine)
        assert [c.name for c in view.columns] == ["id", "name"]
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(view)).scalar() == 2
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP VIEW v_names")


def test_dotted_schema_reflection(engine, people):
    """A ``database.schema`` string addresses another database's schema."""
    with engine.connect() as conn:
        database = engine.dialect._get_default_database_name(conn)
    dotted = "%s.%s" % (database, TEST_SCHEMA)
    insp = inspect(engine)
    assert "people" in insp.get_table_names(schema=dotted)
    assert insp.get_pk_constraint("people", schema=dotted)["constrained_columns"] == \
        ["id"]

    reflected = Table("people", sa.MetaData(), schema=dotted, autoload_with=engine)
    assert "name" in reflected.c


def test_metadata_reflect(engine, people):
    md = sa.MetaData()
    md.reflect(bind=engine)
    assert "people" in md.tables


def test_savepoints_raise(engine, people):
    """No savepoints: begin_nested() fails loudly rather than silently not nesting."""
    with engine.connect() as conn:
        with pytest.raises(NotImplementedError):
            conn.begin_nested()


def test_frostlake_specific_types_round_trip(metadata, engine):
    table = Table("wide", metadata,
                  Column("id", Integer, primary_key=True),
                  Column("ltz", TIMESTAMP_LTZ),
                  Column("ntz", TIMESTAMP_NTZ),
                  Column("dbl", sa.Double),
                  Column("geo", GEOGRAPHY),
                  Column("arr", ARRAY))
    metadata.create_all(engine)
    columns = {c["name"]: c["type"] for c in inspect(engine).get_columns("wide")}
    assert isinstance(columns["ltz"], TIMESTAMP_LTZ)
    assert isinstance(columns["ntz"], TIMESTAMP_NTZ)
    assert isinstance(columns["geo"], GEOGRAPHY)
    assert isinstance(columns["arr"], ARRAY)
    with engine.begin() as conn:
        conn.execute(table.insert().from_select(
            ["id", "ntz", "dbl", "arr"],
            select(sa.literal(1),
                   sa.literal(datetime.datetime(2021, 5, 4, 3, 2, 1)),
                   sa.literal(2.5),
                   sa.literal([1, 2, 3], ARRAY))))
        row = conn.execute(select(table.c.ntz, table.c.dbl, table.c.arr)).one()
    assert row.ntz == datetime.datetime(2021, 5, 4, 3, 2, 1)
    assert row.dbl == 2.5
    assert row.arr == [1, 2, 3]


# -- ORM --------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Person(Base):
    __tablename__ = "orm_people"

    id = Column(Integer, Sequence("orm_people_seq"), primary_key=True)
    name = Column(String(30))
    email = Column(String(50))


def test_orm_crud(engine):
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add_all([Person(name="grace", email="g@x"),
                             Person(name="edsger", email="e@x")])
            session.commit()
            # The Sequence is fetched before the INSERT, so the key is known.
            assert all(p.id is not None for p in session.query(Person))

            person = session.query(Person).filter_by(name="grace").one()
            person.email = "grace@x"
            session.commit()
            assert session.query(Person).filter_by(name="grace").one().email == \
                "grace@x"

            session.delete(person)
            session.commit()
            assert session.query(Person).count() == 1
    finally:
        Base.metadata.drop_all(engine)


def test_orm_relationship_join(engine):
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(Person(name="ken"))
            session.commit()
            rows = session.execute(
                select(Person.name).where(Person.name.startswith("k"))).all()
            assert rows == [("ken",)]
    finally:
        Base.metadata.drop_all(engine)
