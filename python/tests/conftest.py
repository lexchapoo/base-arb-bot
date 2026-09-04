import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Hermetic settings, established before any test module imports arb_bot.
#
# Settings reads `.env` relative to the process working directory, so `pytest` from the
# repo root loaded the developer's real .env while `pytest` from python/ did not -- the
# same suite passed or failed depending on where it was invoked, and the failure
# (test_missing_live_execution_config_is_explicit_blocker) is precisely a test asserting on
# *absent* configuration. Point the loader at a path that cannot exist, and strip any
# ambient variable that maps to a Settings field so an exported shell value cannot do the
# same thing by another route.
os.environ["ARB_BOT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".env.absent")

from arb_bot import config  # noqa: E402  (must follow the env setup above)

# Importing the module already ran `settings = Settings()`, so the pollution is baked into
# that object by the time this line is reached: clearing os.environ afterwards leaves
# os.environ clean and `settings` still holding the hostile value. Rebuild the object and
# copy the clean fields onto it *in place* -- other modules do `from .config import
# settings` and capture this exact object, so rebinding config.settings would fix only the
# importers that had not run yet.
for _field in config.Settings.model_fields:
    os.environ.pop(_field.upper(), None)

_clean = config.Settings()
for _field in config.Settings.model_fields:
    setattr(config.settings, _field, getattr(_clean, _field))

# The suite must never touch a real database.
#
# db/session.py builds its engine from settings.database_url at import time, and the
# default points at the same localhost Postgres the compose file runs. On any machine
# where that database is up -- i.e. any developer's -- handlers under test opened real
# connections and wrote real rows into it: route_trigger alone persists pending events,
# route evaluations and telemetry. That is why the suite intermittently paused inside
# test_pass_exceeding_the_budget_is_abandoned_and_submits_nothing (waiting on the live
# database, not on the evaluation budget) and why `Connection._cancel was never awaited`
# kept surfacing. Point it at a closed port: a refused connection is immediate and loud,
# so accidental database access shows up as a failing test instead of silent writes.
config.settings.database_url = "postgresql+asyncpg://arb:arb@127.0.0.1:1/arb_tests_must_not_connect"
