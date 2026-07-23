from pathlib import Path

import sqlalchemy as sa

from alembic import command
from alembic.config import Config
from app.config import settings


def test_0055_project_asset_governance_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'project-asset-governance.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0054_recipe_content_governance")
    engine = sa.create_engine(database_url)

    def assert_downgraded() -> None:
        inspector = sa.inspect(engine)
        assert "auto_archive_after_days" not in {
            column["name"] for column in inspector.get_columns("media_projects")
        }
        assert "user_asset_metadata" not in inspector.get_table_names()

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        assert "auto_archive_after_days" in {
            column["name"] for column in inspector.get_columns("media_projects")
        }
        assert "ck_media_projects_auto_archive_days_valid" in {
            check["name"] for check in inspector.get_check_constraints("media_projects")
        }
        assert "user_asset_metadata" in inspector.get_table_names()
        assert {
            "uq_user_asset_metadata_user_asset",
            "ix_user_asset_metadata_user_content_hash",
            "ix_user_asset_metadata_user_perceptual_hash",
        } <= {
            index["name"] for index in inspector.get_indexes("user_asset_metadata")
        }
        assert {
            "ck_user_asset_metadata_media_type_valid",
            "ck_user_asset_metadata_analysis_status_valid",
        } <= {
            check["name"]
            for check in inspector.get_check_constraints("user_asset_metadata")
        }
        with engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == (
                "0055_project_asset_governance"
            )

    assert_downgraded()
    command.upgrade(cfg, "0055_project_asset_governance")
    assert_upgraded()
    command.downgrade(cfg, "0054_recipe_content_governance")
    assert_downgraded()
    command.upgrade(cfg, "0055_project_asset_governance")
    assert_upgraded()
    engine.dispose()
