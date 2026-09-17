# WHY this exists (Phase 0 deliberately had no tests/__init__.py, since
# pytest doesn't need one for discovery): mypy needs package boundaries to
# tell tests/conftest.py and tests/data/conftest.py apart as two distinct
# modules rather than a duplicate module name — see tests/data/__init__.py.
