"""bybit_agent — agent-operated Bybit trading skill.

Load .env into os.environ at import time (mirrors the TS env loader) so modules that
read os.environ directly — e.g. config.constants computing proxy-aware REST URLs — see
the configured values regardless of how the process was launched.
"""

from .config.dotenv import load_dotenv as _load_dotenv

_load_dotenv()
