"""
tests/test_check_atomic_release.py — release-discipline gate regression tests
(scripts/check_atomic_release.py, Fase 0.3).

Covers:
  - The pure regex helpers correctly extract RPC call names / RPC function
    definitions from source text (no git dependency).
  - End-to-end: a tiny throwaway git repo proves the script exits 1 when
    tracked code calls an RPC with no tracked migration defining it, and
    exits 0 once the migration is committed alongside the code — the exact
    "atomic release" invariant this script exists to enforce.

Style matches the repo: top-level test_* functions, hand-rolled fixtures.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.check_atomic_release as car

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_atomic_release.py"


# ── Pure regex helpers (no git needed) ───────────────────────────────────

def test_find_rpc_calls_in_text_extracts_names():
    src = '''
resp = sb.rpc("try_acquire_job_lock", {"p_job": job_key}).execute()
other = supabase.rpc('get_daily_api_spend', {"p_client": client_id}).execute()
not_an_rpc_call = "rpc(\\"decoy\\")"  # inside a string literal — still matches naively, expected
'''
    names = car.find_rpc_calls_in_text(src)
    assert "try_acquire_job_lock" in names
    assert "get_daily_api_spend" in names


def test_find_rpc_calls_spanning_multiple_lines_are_still_found():
    """Regression: supabase-py calls are routinely wrapped across lines
    (this is the exact shape of the real call in utils/cost_tracker.py that
    a naive per-line scan missed during development of this checker)."""
    src = '''
resp = supabase_client.rpc(
    "get_daily_api_spend", {"p_client": client_id}
).execute()
'''
    names = car.find_rpc_calls_in_text(src)
    assert "get_daily_api_spend" in names

    with_lines = car.find_rpc_calls_with_lines(src)
    assert ("get_daily_api_spend", 2) in with_lines  # reports the .rpc( line, not the string's line


def test_find_rpc_defs_in_text_extracts_function_names():
    sql = """
    CREATE OR REPLACE FUNCTION increment_daily_sent(inbox_uuid uuid)
    RETURNS integer LANGUAGE sql AS $$ ... $$;

    CREATE FUNCTION release_job_lock(p_job TEXT, p_owner TEXT) RETURNS boolean
    LANGUAGE sql AS $$ ... $$;
    """
    names = car.find_rpc_defs_in_text(sql)
    assert names == {"increment_daily_sent", "release_job_lock"}


def test_find_rpc_defs_in_text_ignores_non_function_sql():
    sql = "CREATE TABLE IF NOT EXISTS job_locks (job_key TEXT PRIMARY KEY);"
    assert car.find_rpc_defs_in_text(sql) == set()


# ── End-to-end: a throwaway git repo proves the invariant ────────────────

def _init_repo(tmp: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, check=True)


def _commit_all(tmp: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "snapshot"], cwd=tmp, check=True)


def _run_checker(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=tmp, capture_output=True, text=True,
    )


def test_fails_when_code_calls_undefined_rpc():
    """The exact regression this script prevents: code committed, migration not."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _init_repo(tmp)
        (tmp / "worker.py").write_text(
            'sb.rpc("try_acquire_job_lock", {"p_job": "warmup_engine"}).execute()\n'
        )
        _commit_all(tmp)  # migration was never written/committed at all

        result = _run_checker(tmp)

        assert result.returncode == 1, result.stdout
        assert "try_acquire_job_lock" in result.stdout
        assert "worker.py" in result.stdout


def test_passes_when_migration_lands_in_same_commit():
    """Atomic release: code + its backing migration committed together."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _init_repo(tmp)
        (tmp / "worker.py").write_text(
            'sb.rpc("try_acquire_job_lock", {"p_job": "warmup_engine"}).execute()\n'
        )
        api_dir = tmp / "api"
        api_dir.mkdir()
        (api_dir / "some_migration.sql").write_text(
            "CREATE OR REPLACE FUNCTION try_acquire_job_lock(p_job TEXT) "
            "RETURNS boolean LANGUAGE sql AS $$ SELECT true; $$;\n"
        )
        _commit_all(tmp)

        result = _run_checker(tmp)

        assert result.returncode == 0, result.stdout
        assert "OK" in result.stdout


def test_passes_when_no_rpc_calls_exist():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _init_repo(tmp)
        (tmp / "worker.py").write_text("print('no rpc calls here')\n")
        _commit_all(tmp)

        result = _run_checker(tmp)

        assert result.returncode == 0, result.stdout


def test_uncommitted_migration_does_not_count():
    """A migration sitting on disk but not committed must NOT satisfy the
    check — this is precisely the bug the script exists to catch (mirrors
    the real repo's pre-fix state: code committed, migration left as an
    untracked file)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _init_repo(tmp)
        (tmp / "worker.py").write_text(
            'sb.rpc("get_daily_api_spend", {"p_client": None}).execute()\n'
        )
        _commit_all(tmp)  # commit the code only

        api_dir = tmp / "api"
        api_dir.mkdir()
        (api_dir / "some_migration.sql").write_text(
            "CREATE OR REPLACE FUNCTION get_daily_api_spend(p_client TEXT) "
            "RETURNS numeric LANGUAGE sql AS $$ SELECT 0; $$;\n"
        )
        # Deliberately NOT committed — stays untracked, like api/critical5_critical6_migration.sql today.

        result = _run_checker(tmp)

        assert result.returncode == 1, result.stdout
        assert "get_daily_api_spend" in result.stdout


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
