#!/usr/bin/env python3
"""Main entry point for Inventory Guard - Ansible inventory semantic guard."""

from __future__ import annotations

import logging
import sys

try:
    from . import compare, config, output
except ImportError:
    # Handle direct execution
    import compare  # type: ignore
    import config  # type: ignore
    import output  # type: ignore

logger = logging.getLogger(__name__)


def validate_thresholds(summary: compare.Summary, args) -> None:
    """
    Validate that changes are within configured thresholds.

    Args:
        summary: Comparison results
        args: Configuration with threshold limits

    Raises:
        SystemExit: Exits with code 2 if thresholds are exceeded
    """
    # Check host change percentage
    if summary.host_delta_pct > args.max_host_change_pct:
        logger.error(
            "Guard check failed: Host delta %d (%.2f%%) exceeds limit %.1f%%",
            summary.host_delta,
            summary.host_delta_pct,
            args.max_host_change_pct,
        )
        sys.exit(2)

    # Check absolute host change limit
    if args.max_host_change_abs and summary.host_delta > args.max_host_change_abs:
        logger.error(
            "Guard check failed: Host delta %d exceeds absolute cap %d",
            summary.host_delta,
            args.max_host_change_abs,
        )
        sys.exit(2)

    # Check variable change percentage
    if summary.var_change_pct > args.max_var_change_pct:
        logger.error(
            "Guard check failed: Variable changes %d (%.2f%%) exceed limit %.1f%%",
            summary.var_changes_total,
            summary.var_change_pct,
            args.max_var_change_pct,
        )
        sys.exit(2)

    # Check absolute variable change limit
    if args.max_var_change_abs and summary.var_changes_total > args.max_var_change_abs:
        logger.error(
            "Guard check failed: Variable changes %d exceed absolute cap %d",
            summary.var_changes_total,
            args.max_var_change_abs,
        )
        sys.exit(2)

    logger.info("All checks passed")


def main() -> None:
    """Main entry point - orchestrates the inventory comparison workflow."""
    # Parse configuration
    args = config.parse_and_merge()

    # Setup logging based on verbosity
    config.setup_logging(args.verbose)

    # Run comparison
    try:
        summary = compare.run_comparison(args)
    except FileNotFoundError as e:
        logger.error("File not found: %s", e)
        sys.exit(1)
    except (ValueError, TypeError) as e:
        logger.error("Invalid input: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error during comparison: %s", e)
        sys.exit(1)

    # Generate outputs
    try:
        if args.json:
            output.output_json_stdout(summary)

        if args.json_out:
            output.write_json_file(summary, args.json_out)

        if args.report:
            output.write_markdown_report(summary, args.report)
    except Exception as e:
        logger.error("Failed to write output: %s", e)
        sys.exit(1)

    # Validate thresholds
    validate_thresholds(summary, args)

    # Success
    sys.exit(0)


if __name__ == "__main__":
    main()
