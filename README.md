# frostlake-sqlalchemy

A [SQLAlchemy](https://www.sqlalchemy.org/) dialect for [Frostlake](https://frostlake.dev),
riding the pure-stdlib [`frostlake`](https://pypi.org/project/frostlake/) DB-API driver over
the engine's HTTP protocol. Core, ORM and reflection all work; no JVM, no JDBC.

```python
from sqlalchemy import create_engine

engine = create_engine("frostlake://localhost:18082/MY_DB?schema=PUBLIC")
```

## Requirements

- SQLAlchemy **2.0** or newer
- the `frostlake` driver **0.2.1** or newer (installed as a dependency)
- a Frostlake engine **0.0.7** or newer, reachable over HTTP

## Install

```bash
pip install frostlake-sqlalchemy
```

## Connection URL

```
frostlake://[host[:port]][/DATABASE][?schema=SCHEMA&timeout=SECONDS]
```

Host defaults to `localhost`, port to `18082`. The database and schema become `USE`
statements on the session before the first statement. The protocol carries no
credentials, so a username or password in the URL is ignored (with a warning).

```python
create_engine("frostlake://")                                # localhost:18082
create_engine("frostlake://db.internal:18082/ANALYTICS?schema=STAGING")
```

## Quick start

```python
from sqlalchemy import Column, Integer, MetaData, Sequence, String, Table, select

md = MetaData()
people = Table(
    "people", md,
    Column("id", Integer, Sequence("people_id_seq"), primary_key=True),
    Column("name", String(50), nullable=False, comment="display name"),
)
md.create_all(engine)

with engine.begin() as conn:
    conn.execute(people.insert(), [{"name": "ada"}, {"name": "alan"}])
    for row in conn.execute(select(people).order_by(people.c.name)):
        print(row.id, row.name)
```

## Identifier case

Frostlake follows Snowflake: an unquoted identifier folds to upper case, a quoted one
keeps its case. The dialect normalizes both ways, so lower-case names in Python address
the upper-case objects in the database, and reflection hands lower-case names back:

| In Python | In SQL | Stored as |
| --- | --- | --- |
| `Table("people", ...)` | `people` | `PEOPLE` |
| `Table("People", ...)` | `"People"` | `People` |

The 64 words the engine actually rejects in an identifier position are quoted
automatically; they are listed in `frostlake_sqlalchemy.RESERVED_WORDS`.

## Types

| SQLAlchemy | Frostlake | Notes |
| --- | --- | --- |
| `Integer`, `BigInteger`, `SmallInteger` | `INTEGER`, `BIGINT`, `SMALLINT` | all stored as `NUMBER(38, 0)` |
| `Numeric(p, s)`, `NUMBER(p, s)` | `NUMBER(p, s)` | exact; reads back as `Decimal` |
| `Float`, `Double`, `REAL` | `FLOAT`, `DOUBLE`, `REAL` | one binary float type underneath |
| `String(n)`, `Text`, `Unicode` | `VARCHAR(n)`, `VARCHAR` | |
| `Boolean` | `BOOLEAN` | native |
| `Date`, `Time` | `DATE`, `TIME` | |
| `DateTime` | `TIMESTAMP_NTZ` | `DateTime(timezone=True)` gives `TIMESTAMP_TZ` |
| `TIMESTAMP_NTZ`, `TIMESTAMP_LTZ`, `TIMESTAMP_TZ` | same | for picking one explicitly |
| `LargeBinary` | `BINARY` | reads back as `bytes` |
| `JSON`, `VARIANT`, `OBJECT`, `ARRAY` | `VARIANT`, `OBJECT`, `ARRAY` | see below |
| `GEOGRAPHY`, `GEOMETRY` | same | handled as text |
| `Enum` | `VARCHAR(n)` | no native enum |
| `Uuid` | `CHAR(32)` | no native uuid |

The Frostlake-specific ones import from the package:

```python
from frostlake_sqlalchemy import VARIANT, OBJECT, ARRAY, TIMESTAMP_LTZ
```

Note `frostlake_sqlalchemy.ARRAY` shadows `sqlalchemy.ARRAY` — they model different
things. Frostlake's is an untyped semi-structured array; SQLAlchemy's generic one is a
typed, dimensioned SQL array the engine has no equivalent for.

## Semi-structured data

`VARIANT`, `OBJECT` and `ARRAY` behave like SQLAlchemy's `JSON` type — documents go in as
Python objects and come back as Python objects, and `col["key"]` indexing works
(compiled to `GET()` / `GET_PATH()`). `Column(JSON)` is a `VARIANT` column.

**One limitation, inherited from the engine**: a `VALUES` clause takes no function calls,
and a VARIANT value needs `PARSE_JSON()` around it. So a semi-structured column has to be
written with `INSERT ... SELECT`:

```python
# works
conn.execute(docs.insert().from_select(
    ["id", "payload"],
    select(literal(1), literal({"k": [1, 2]}, VARIANT))))

# rejected by the engine: INSERT INTO docs (id, payload) VALUES (?, PARSE_JSON(?))
conn.execute(docs.insert().values(id=1, payload={"k": [1, 2]}))
```

Reading is unrestricted:

```python
conn.execute(select(docs.c.payload["k"]))        # GET(docs.payload, 'k')
conn.execute(select(docs.c.payload[("a", "b")])) # GET_PATH(docs.payload, 'a.b')
```

## Primary keys

Frostlake has no `RETURNING` and no last-insert-id, so a server-generated key cannot be
read back after an `INSERT`. **Give an ORM primary key a `Sequence`**: the dialect fetches
the next value before the insert, so the object comes back with its key populated.

```python
class Person(Base):
    __tablename__ = "people"
    id = Column(Integer, Sequence("people_id_seq"), primary_key=True)
    name = Column(String(30))
```

Without one, an integer primary key gets `AUTOINCREMENT` in the DDL and the engine fills
it in — fine for Core inserts and bulk loads, but the ORM will not know the value it was
given. `Identity(start=..., increment=...)` is supported too, with the same caveat.

## Table options

```python
Table("events", md,
      Column("day", Date),
      Column("kind", String(20)),
      frostlake_clusterby=["day", "kind"])   # CLUSTER BY (day, kind)
```

`Inspector.get_table_options()` reflects it back. Table and column `comment=` are
supported in both directions.

## Reflection

`get_table_names`, `get_view_names`, `get_view_definition`, `get_columns`,
`get_pk_constraint`, `get_foreign_keys`, `get_unique_constraints`, `get_table_comment`,
`get_table_options`, `get_schema_names`, `get_sequence_names`, `has_table`,
`has_sequence` — plus `Table(..., autoload_with=engine)` and `MetaData.reflect()`.

A schema may be given as `"database.schema"` to reach another database:

```python
inspect(engine).get_table_names(schema="OTHER_DB.PUBLIC")
Table("t", md, autoload_with=engine, schema="OTHER_DB.PUBLIC")
```

Constraints the engine named itself (`SYS_CONSTRAINT_<uuid>`) are reported with
`name: None`, so a reflected table does not carry one server's random names.

Two gaps worth knowing:

- `get_indexes()` is always `[]` and `get_check_constraints()` is always `[]` — the engine
  parses `CHECK` but does not store it in `INFORMATION_SCHEMA.CHECK_CONSTRAINTS`.
- identity seed and step are read from `DESCRIBE TABLE`, because
  `INFORMATION_SCHEMA.COLUMNS` reports `1`/`1` for every identity column on engine 0.0.7.

## Transactions

Connections are transactional the way SQLAlchemy expects: `engine.begin()`,
`connection.commit()` and `connection.rollback()` all work. Two isolation levels are
accepted:

```python
with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    ...
```

`READ COMMITTED` is the default. **There are no savepoints**, so
`Session.begin_nested()` and `connection.begin_nested()` raise `NotImplementedError`.

## What the engine does not have

| Feature | Behaviour |
| --- | --- |
| indexes | `CreateIndex` raises `CompileError`; drop `index=True` from your models |
| savepoints / nested transactions | `NotImplementedError` |
| `SELECT ... FOR UPDATE` | the clause is dropped; there is no row locking |
| `RETURNING` | not emitted; use a `Sequence` for generated keys |
| `DEFERRABLE` constraints | the clause is not emitted |
| `INSERT` with no values at all | needs at least one named column — same as Oracle |
| `INTERVAL` columns | no such column type |

Two Snowflake-compatible behaviours that differ from most other backends:

- `regexp_match()` compiles to `RLIKE`, which matches the **whole** value. `"a.a"` matches
  `ada`; `"a"` does not. Anchor-free searching needs `.*` on both ends.
- `10 / 3` is `3.333333`, not `3` — division is never integer division.

## Tests

```bash
pip install -e ".[test]"
pytest tests/test_compile.py        # no server, no JVM
```

The live tests boot an engine themselves from a classpath:

```bash
JAVA_HOME=~/.jdks/liberica-17 FROSTLAKE_CLASSPATH="<engine jar + deps>" pytest
```

With `FROSTLAKE_CLASSPATH` unset the live tests skip and the compile tests still run, so a
missing engine never shows up as a false pass.

## License

Apache-2.0.
