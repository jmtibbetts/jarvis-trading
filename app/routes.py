"""Compatibility shim (Phase 7): the monolith moved to app/routers/*.
Everything importable from here before the split stays importable —
main.py mounts `router`, the scheduler imports `log_decision`, tests
import route functions and helpers by name.
"""
from app.routers.common import *  # noqa: F401,F403
from app.routers.common import _CONFIDENCE_WORDS, _CONGRESS_DISCLAIMER, _FOCUS_SCANS, _FOCUS_SCAN_LOCK, _asset_dict, _build_crypto_markets, _build_fx_rates, _build_provider_status, _config_dict, _congress_trade_dict, _context_terms, _focus_signal_ids, _holdings_for_period, _ingestion_run_dict, _insider_tx_dict, _institutional_disclaimer, _institutional_periods, _is_crypto_like_signal, _is_pending_equity_candidate, _news_dict, _parse_datetime, _position_dict, _prior_quarter_end, _related_signal_context, _select_comparison_periods, _sig_dict, _signal_evaluation_dict, _source_health_dict, _telegram_setup_credentials, _threat_dict, _ui_build  # noqa: F401,E501
from app.routers.trading import *  # noqa: F401,F403
from app.routers.learning import *  # noqa: F401,F403
from app.routers.intel import *  # noqa: F401,F403
from app.routers.platform import *  # noqa: F401,F403

# LAST on purpose: every domain module also exports a name `router` (its
# own sub-router), and star imports above would otherwise leave whichever
# module imported last as this module's `router`. The aggregate must win —
# main.py mounts what this name points to.
from app.routers import router  # noqa: F401,E402
