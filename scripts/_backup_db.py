"""Standalone WAL-safe database backup entry point for the PowerShell scripts.

Invoked as::

    python scripts/_backup_db.py <db_paths.py> <source_db> <dest_file>

This exists as a real file rather than a ``python -c "<inline script>"`` string
because Windows PowerShell 5.1 - which the scheduled task and ``run-hidden.vbs``
launch - strips embedded double quotes when passing a ``-c`` argument to a
native exe, corrupting the inline script so it never runs. A file has no such
quoting hazard. See ``scripts/Resolve-DbPath.ps1::Backup-SalesBuddyDb``.

Loads ``db_paths.py`` standalone (it imports only the stdlib) instead of the
Flask app package, so it stays safe to call from a scheduled task / SYSTEM
context. Exit code 0 on success, 1 on failure.
"""
import importlib.util
import sys


def main() -> int:
    if len(sys.argv) != 4:
        return 1
    db_paths_file, source_db, dest_file = sys.argv[1], sys.argv[2], sys.argv[3]
    spec = importlib.util.spec_from_file_location('db_paths', db_paths_file)
    if spec is None or spec.loader is None:
        return 1
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ok = mod.backup_database(dest_file, src=source_db)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
