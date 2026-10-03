import os
import tempfile

# Test-Umgebung, bevor app.config importiert wird
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ.setdefault("HA_URL", "http://ha.test")
os.environ.setdefault("HA_TOKEN", "dummy")
os.environ.setdefault("SMTP_HOST", "smtp.test")
os.environ.setdefault("SMTP_FROM", "abrechnung@test.de")
os.environ["VICTRON_LOGGER"] = "0"  # Hintergrund-Logger in Tests nicht starten

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Jeder Test startet mit leerer Datenbank."""
    from app import db

    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    yield
