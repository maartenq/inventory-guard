"""Output and reporting functions for inventory comparison results."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------- JSON Helpers ----------
def _json_default(o: Any) -> Any:
    """JSON serializer for dataclasses and sets."""
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


# ---------- Output Functions ----------
def output_json_stdout(summary: Any) -> None:
    """
    Write JSON summary to stdout.

    This is the primary machine-readable output for piping to other tools.
    """
    print(
        json.dumps(
            summary,
            default=_json_default,
            indent=2,
            ensure_ascii=False,
        )
    )


def write_json_file(summary: Any, path: str) -> None:
    """
    Write JSON summary to file.

    Args:
        summary: Comparison results
        path: File path to write to

    Raises:
        OSError: If file cannot be written
    """
    logger.info("Writing JSON summary to %s", path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            summary,
            f,
            default=_json_default,
            indent=2,
            ensure_ascii=False,
        )


def write_markdown_report(summary: Any, path: str) -> None:
    """
    Write Markdown report to file.

    Args:
        summary: Comparison results
        path: File path to write to

    Raises:
        OSError: If file cannot be written
    """
    logger.info("Writing Markdown report to %s", path)
    summary_dict = json.loads(json.dumps(summary, default=_json_default))
    md = _render_markdown(summary_dict)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)


def _render_markdown(summary: dict[str, Any]) -> str:
    """
    Generate Markdown report from summary dictionary.

    Args:
        summary: Dictionary representation of Summary object

    Returns:
        Markdown formatted string
    """
    lines = []
    lines.append("# Inventory Semantic Summary\n")
    lines.append(f"- **Current hosts**: {summary['current_hosts']}")
    lines.append(f"- **New hosts**: {summary['new_hosts']}")
    lines.append(f"- **Host delta**: {summary['host_delta']} ({summary['host_delta_pct']}%)\n")

    if summary["host_added"]:
        lines.append("## Hosts Added")
        for h in summary["host_added"]:
            lines.append(f"- `{h}`")
        lines.append("")

    if summary["host_removed"]:
        lines.append("## Hosts Removed")
        for h in summary["host_removed"]:
            lines.append(f"- `{h}`")
        lines.append("")

    lines.append("## Variable Changes (across common hosts)")
    lines.append(
        f"- **Total var changes**: {summary['var_changes_total']} ({summary['var_change_pct']}%)"
    )
    lines.append(f"- **Baseline var keys**: {summary['var_baseline_keys']}\n")

    if summary.get("sample_per_host_changes"):
        lines.append("### Sample per-host changes")
        for host, changes in summary["sample_per_host_changes"].items():
            lines.append(f"- **{host}**")
            if changes["added_keys"]:
                lines.append(f"  - added: {', '.join(f'`{k}`' for k in changes['added_keys'])}")
            if changes["removed_keys"]:
                lines.append(f"  - removed: {', '.join(f'`{k}`' for k in changes['removed_keys'])}")
            if changes["changed_values"]:
                lines.append(
                    f"  - value changes: {', '.join(f'`{k}`' for k in changes['changed_values'])}"
                )
        lines.append("")

    lim = summary["limits"]
    lines.append("## Limits")
    lines.append(f"- max_host_change_pct: {lim['max_host_change_pct']}")
    lines.append(f"- max_var_change_pct: {lim['max_var_change_pct']}")
    lines.append(f"- max_host_change_abs: {lim['max_host_change_abs']}")
    lines.append(f"- max_var_change_abs: {lim['max_var_change_abs']}")
    if lim["ignored_key_regex"]:
        lines.append(
            f"- ignored_key_regex: {', '.join(f'`{p}`' for p in lim['ignored_key_regex'])}"
        )
    lines.append("")
    return "\n".join(lines)
