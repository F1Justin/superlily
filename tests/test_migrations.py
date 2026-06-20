from alembic.config import Config
from alembic.script import ScriptDirectory


def test_revision_identifiers_fit_alembic_version_column() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    revisions = list(scripts.walk_revisions())
    assert revisions
    assert all(len(revision.revision) <= 32 for revision in revisions)
