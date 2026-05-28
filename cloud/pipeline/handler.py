"""
handler.py
==========
AWS Lambda entry point for the Meta-Analyst daily pipeline.

AWS Step Functions invokes this Lambda once per pipeline step, passing
a JSON payload: {"step": "<name>"}. The handler imports the matching
pipeline module and calls its entry-point function.

Step → module mapping:
  ohlc         → update_ohlc_s3.run_update()
  ratings      → update_ratings_s3.run_update()
  sectors      → bootstrap_sectors_s3.run()
  fundamentals → update_fundamentals_s3.run_update()
  insider      → update_insider_s3.run_update()
  marts        → compute_marts.run_compute()
  validate     → validate_data.main()
  backtest     → backtest_v2.main()
  alerts       → send_alerts.run()
"""

import importlib
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Maps the Step Functions payload key → (module_name, function_name)
STEP_MAP = {
    "ohlc"        : ("update_ohlc_s3",         "run_update"),
    "ratings"     : ("update_ratings_s3",      "run_update"),
    "sectors"     : ("bootstrap_sectors_s3",   "run"),
    "fundamentals": ("update_fundamentals_s3", "run_update"),
    "insider"     : ("update_insider_s3",      "run_update"),
    "marts"       : ("compute_marts",          "run_compute"),
    "validate"    : ("validate_data",          "main"),
    "backtest"    : ("backtest_v2",            "main"),
    "alerts"      : ("send_alerts",            "run"),
}


def lambda_handler(event, context):
    """
    Invoked by Step Functions with: {"step": "<name>"}

    Returns {"step": "<name>", "status": "ok"} on success.
    Raises on failure — Step Functions catches the error and fails the execution.
    """
    step = event.get("step")

    if not step:
        raise ValueError("Missing 'step' key in Lambda event payload.")
    if step not in STEP_MAP:
        raise ValueError(
            f"Unknown pipeline step: '{step}'. "
            f"Valid steps: {sorted(STEP_MAP)}"
        )

    module_name, func_name = STEP_MAP[step]
    logger.info(f"[PIPELINE] step={step}  →  {module_name}.{func_name}()")

    module = importlib.import_module(module_name)
    fn = getattr(module, func_name)
    fn()

    logger.info(f"[PIPELINE] step={step} completed OK.")
    return {"step": step, "status": "ok"}
