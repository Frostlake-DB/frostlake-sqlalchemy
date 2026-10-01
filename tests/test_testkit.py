"""The engine's testkit corpus, replayed through the dialect by testkit_runner.py.

Runs only with FL_CORPUS naming frostlake's engine/src/test/resources/testkit, against an
engine from FROSTLAKE_CLASSPATH as the live tests take theirs (or FROSTLAKE_URL, for one
already running); it skips without them.
"""

import importlib.util
import os
import pathlib

import pytest

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "testkit_runner.py"


@pytest.mark.skipif(not os.environ.get("FL_CORPUS"),
                    reason="set FL_CORPUS to frostlake's engine/src/test/resources/testkit"
                           " to replay the testkit corpus")
def test_corpus_replays_without_failures():
    spec = importlib.util.spec_from_file_location("testkit_runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.suites_directory()   # an FL_CORPUS without suites fails here, before any engine
    if not (os.environ.get("FROSTLAKE_CLASSPATH") or os.environ.get("FROSTLAKE_URL")):
        pytest.skip("no engine (set FROSTLAKE_CLASSPATH or FROSTLAKE_URL)")
    # The runner prints its tally and first failures, and returns 1 on a failed or errored
    # case.
    assert runner.main(["--backend", "sqlalchemy"]) == 0
