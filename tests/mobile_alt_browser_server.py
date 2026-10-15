"""Mobile layout fixture with the legacy setups populated by the shared fixture."""
from tests.redesign_browser_server import app, settings

settings.alt_config.engine_version = "v1"
