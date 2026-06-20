"""Platform settings round-trip + video cost-estimate logic."""
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (register tables)
from app.db import Base
from app.routers.generate import _estimate_cost
from app.services import config_store


def make_session():
    _, path = tempfile.mkstemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_settings_roundtrip():
    db = make_session()
    assert config_store.get_setting(db, "image_n") == 4          # default
    assert config_store.get_setting(db, "reverse_prompt_enabled") is True

    config_store.set_setting(db, "image_n", 6)
    config_store.set_setting(db, "reverse_prompt_enabled", False)
    assert config_store.get_setting(db, "image_n") == 6
    assert config_store.get_setting(db, "reverse_prompt_enabled") is False

    assert config_store.get_setting(db, "unknown_key", "fallback") == "fallback"


class _Model:
    def __init__(self, cost, extra):
        self.cost_credits = cost
        self.extra = extra


def test_video_cost_estimate():
    # explicit preview_cost
    m = _Model(50, {"preview_cost": 5})
    assert _estimate_cost(m, "video", "preview") == 5
    assert _estimate_cost(m, "video", "final") == 50
    # no preview_cost -> max(1, base//10)
    m2 = _Model(50, None)
    assert _estimate_cost(m2, "video", "preview") == 5
    # image always full cost
    assert _estimate_cost(m, "image", "preview") == 50


def test_public_config_exposes_video_duration_limit(client, make_user, auth):
    make_user("13900000180", balance=1000)
    h = auth("13900000180")
    r = client.get("/api/config", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["video_duration_max_seconds"] == 900
