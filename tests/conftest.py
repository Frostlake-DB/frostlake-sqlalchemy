"""Fixtures shared by the test modules.

The compile-only tests need nothing. The live tests need a Frostlake engine, which is
booted here from ``FROSTLAKE_CLASSPATH`` (and ``JAVA_HOME``, if java is not on PATH) --
the same contract the driver repos use. With the variable unset the live tests skip
rather than silently pass.
"""

import os
import socket
import subprocess
import time
import urllib.request

import pytest
import sqlalchemy as sa

import frostlake_sqlalchemy  # noqa: F401  (registers the frostlake:// dialect)

TEST_DATABASE = "sa_test_db"
TEST_SCHEMA = "sa_test_schema"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    classpath = os.environ.get("FROSTLAKE_CLASSPATH")
    if not classpath:
        pytest.skip("FROSTLAKE_CLASSPATH not set")
    java = (os.path.join(os.environ["JAVA_HOME"], "bin", "java")
            if os.environ.get("JAVA_HOME") else "java")
    port = _free_port()
    # The engine keeps stages under ~/.frostlake_stages, its catalog under
    # SQL_ENGINE_DATA_DIR and writes db-engine.log into its cwd: all three
    # are pointed at a private directory so a run neither inherits what an
    # earlier one left behind nor litters the real home or this checkout.
    home = tmp_path_factory.mktemp("engine-home")
    env = dict(os.environ, SQL_ENGINE_DATA_DIR=str(home / "data"))
    process = subprocess.Popen(
        [java, "-Duser.home=" + str(home), "-cp", classpath,
         "dev.frostlake.http.DatabaseHttpServer", str(port)],
        cwd=str(home), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(150):
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("engine did not come up on %s" % base)
        yield "frostlake://127.0.0.1:%d" % port
    finally:
        process.kill()


@pytest.fixture(scope="session")
def engine(server_url):
    """An engine pointed at a freshly created database and schema."""
    bootstrap = sa.create_engine(server_url)
    with bootstrap.begin() as conn:
        conn.exec_driver_sql("CREATE OR REPLACE DATABASE " + TEST_DATABASE)
        conn.exec_driver_sql("USE DATABASE " + TEST_DATABASE)
        conn.exec_driver_sql("CREATE OR REPLACE SCHEMA " + TEST_SCHEMA)
    bootstrap.dispose()

    engine = sa.create_engine(
        "%s/%s?schema=%s" % (server_url, TEST_DATABASE, TEST_SCHEMA))
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine):
    with engine.connect() as conn:
        yield conn


@pytest.fixture
def metadata(engine):
    """MetaData whose tables are dropped again when the test ends."""
    md = sa.MetaData()
    yield md
    md.drop_all(engine, checkfirst=True)
