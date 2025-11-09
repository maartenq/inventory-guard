"""Configuration and argument parsing for Inventory Guard."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tomllib
from pathlib import Path
from typing import Any

# ---------- Defaults ----------
DEFAULT_MAX_HOST_CHANGE_PCT = 5.0
DEFAULT_MAX_VAR_CHANGE_PCT = 2.0
DEFAULT_MAX_HOST_CHANGE_ABS = 0
DEFAULT_MAX_VAR_CHANGE_ABS = 0
DEFAULT_SETLIKE_KEYS = [r"^foreman_host_collections$"]
DEFAULT_CONFIG_FILE = "inventory_semantic_guard.toml"


# ---------- Logging ----------
def setup_logging(verbosity: int) -> None:
    """
    Configure logging based on verbosity level.

    Args:
        verbosity: 0 = WARNING, 1 = INFO, 2+ = DEBUG
    """
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    # JSON-structured logging for easier parsing
    class JSONFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log_obj = {
                "timestamp": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            if record.exc_info:
                log_obj["exception"] = self.formatException(record.exc_info)
            return json.dumps(log_obj)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))

    logging.root.setLevel(level)
    logging.root.handlers.clear()
    logging.root.addHandler(handler)


# ---------- Config Loading ----------
def load_config(path_opt: str | None) -> dict[str, Any]:
    """
    Load a TOML config. If 'path_opt' is given, use it. Otherwise try the
    default file name in CWD. Returns {} if no file is found.
    """
    if path_opt:
        p = Path(path_opt)
        if not p.is_file():
            raise FileNotFoundError(f"Config not found: {p}")
        with p.open("rb") as f:
            return tomllib.load(f)

    # Auto-discover default file if present
    p = Path.cwd() / DEFAULT_CONFIG_FILE
    if p.is_file():
        with p.open("rb") as f:
            return tomllib.load(f)
    return {}


# ---------- CLI ----------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Build CLI arguments. Many defaults are None so we can merge config
    values later (config -> CLI-defaults), and keep CLI explicit args
    highest precedence.
    """
    ap = argparse.ArgumentParser(
        description="Semantic guard for Ansible inventory changes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--config", default="", help="Path to a TOML config file")
    ap.add_argument("--current", default="", help="Path to current inventory")
    ap.add_argument("--new", default="", help="Path to candidate inventory")
    ap.add_argument(
        "--max-host-change-pct",
        type=float,
        default=None,
        help=f"Max %% host churn vs current (default {DEFAULT_MAX_HOST_CHANGE_PCT})",
    )
    ap.add_argument(
        "--max-var-change-pct",
        type=float,
        default=None,
        help=f"Max %% var key changes (default {DEFAULT_MAX_VAR_CHANGE_PCT})",
    )
    ap.add_argument(
        "--max-host-change-abs",
        type=int,
        default=None,
        help=f"Absolute host churn cap (default {DEFAULT_MAX_HOST_CHANGE_ABS})",
    )
    ap.add_argument(
        "--max-var-change-abs",
        type=int,
        default=None,
        help=f"Absolute var change cap (default {DEFAULT_MAX_VAR_CHANGE_ABS})",
    )
    ap.add_argument(
        "--ignore-key-regex",
        action="append",
        default=None,
        help="Regex for volatile var keys to ignore (repeatable)",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output JSON summary to stdout",
    )
    ap.add_argument("--json-out", default=None, help="Write JSON summary to path")
    ap.add_argument("--report", default=None, help="Write Markdown report to path")
    ap.add_argument(
        "--set-like-key-regex",
        action="append",
        default=None,
        help=(
            "Keys to treat as unordered sets if value is list-of-scalars "
            f"(default {DEFAULT_SETLIKE_KEYS!r})"
        ),
    )
    return ap.parse_args(argv)


def merge_with_config(ns: argparse.Namespace) -> argparse.Namespace:
    """
    Merge CLI args with TOML config and built-in defaults.
    Precedence: CLI explicit > config > built-in defaults.
    """
    cfg = load_config(ns.config or "")

    def get_cfg(key: str, default: Any = None) -> Any:
        # allow both flat and a simple [inventory_guard] table
        if key in cfg:
            return cfg[key]
        table = cfg.get("inventory_guard", {})
        if isinstance(table, dict) and key in table:
            return table[key]
        return default

    def pick(value_cli, key_cfg: str, default_val):
        return value_cli if value_cli is not None else get_cfg(key_cfg, default_val)

    current = ns.current or get_cfg("current", "")
    new = ns.new or get_cfg("new", "")

    max_host_change_pct = pick(
        ns.max_host_change_pct,
        "max_host_change_pct",
        DEFAULT_MAX_HOST_CHANGE_PCT,
    )
    max_var_change_pct = pick(
        ns.max_var_change_pct, "max_var_change_pct", DEFAULT_MAX_VAR_CHANGE_PCT
    )
    max_host_change_abs = pick(
        ns.max_host_change_abs,
        "max_host_change_abs",
        DEFAULT_MAX_HOST_CHANGE_ABS,
    )
    max_var_change_abs = pick(
        ns.max_var_change_abs, "max_var_change_abs", DEFAULT_MAX_VAR_CHANGE_ABS
    )

    ignore_key_regex = (
        ns.ignore_key_regex
        if ns.ignore_key_regex is not None
        else get_cfg("ignore_key_regex", [])
    )
    set_like_key_regex = (
        ns.set_like_key_regex
        if ns.set_like_key_regex is not None
        else get_cfg("set_like_key_regex", DEFAULT_SETLIKE_KEYS)
    )

    json_out = ns.json_out if ns.json_out is not None else get_cfg("json_out", "")
    report = ns.report if ns.report is not None else get_cfg("report", "")

    # Verbosity and json flags are CLI-only (not configurable via TOML)
    verbose = getattr(ns, "verbose", 0)
    output_json = getattr(ns, "json", False)

    # Validate required paths
    if not current:
        raise SystemExit("--current is required (CLI or TOML)")
    if not new:
        raise SystemExit("--new is required (CLI or TOML)")

    merged = argparse.Namespace(
        current=current,
        new=new,
        max_host_change_pct=float(max_host_change_pct),
        max_var_change_pct=float(max_var_change_pct),
        max_host_change_abs=int(max_host_change_abs),
        max_var_change_abs=int(max_var_change_abs),
        ignore_key_regex=list(ignore_key_regex or []),
        set_like_key_regex=list(set_like_key_regex or []),
        json_out=str(json_out or ""),
        report=str(report or ""),
        verbose=int(verbose),
        json=bool(output_json),
    )
    return merged


def parse_and_merge(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse arguments and merge with configuration. Convenience function."""
    args_cli = parse_args(argv)
    return merge_with_config(args_cli)
