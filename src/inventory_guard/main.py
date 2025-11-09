#!/usr/bin/env python3
# inventory_semantic_guard.py

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML  # type: ignore
from ruamel.yaml.nodes import ScalarNode

yaml = YAML(typ="safe")


# ---------- Defaults ----------
DEFAULT_MAX_HOST_CHANGE_PCT = 5.0
DEFAULT_MAX_VAR_CHANGE_PCT = 2.0
DEFAULT_MAX_HOST_CHANGE_ABS = 0
DEFAULT_MAX_VAR_CHANGE_ABS = 0
DEFAULT_SETLIKE_KEYS = [r"^foreman_host_collections$"]
DEFAULT_CONFIG_FILE = "inventory_semantic_guard.toml"


# --- Allow Ansible !vault tags as plain strings ---
def _vault_constructor(loader, node):
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    return loader.construct_object(node)


yaml.constructor.add_constructor("!vault", _vault_constructor)

# ---------- Types ----------
VarsMap = dict[str, Any]
HostVars = dict[str, VarsMap]
YAMLNode = Mapping[str, Any]


@dataclass(slots=True)
class Limits:
    max_host_change_pct: float
    max_var_change_pct: float
    max_host_change_abs: int
    max_var_change_abs: int
    ignored_key_regex: list[str]


@dataclass(slots=True)
class Summary:
    current_hosts: int
    new_hosts: int
    host_added: list[str]
    host_removed: list[str]
    host_delta: int
    host_delta_pct: float

    var_changes_total: int
    var_change_pct: float
    var_baseline_keys: int

    limits: Limits
    sample_per_host_changes: dict[str, dict[str, list[str]]]


# ---------- IO ----------
def load_yaml(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping at root")
    return dict(data)


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


# ---------- Core helpers ----------
def _merge(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> VarsMap:
    out: VarsMap = dict(a or {})
    if b:
        out.update(b)
    return out


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = []
    lines.append("# Inventory Semantic Summary\n")
    lines.append(f"- **Current hosts**: {summary['current_hosts']}")
    lines.append(f"- **New hosts**: {summary['new_hosts']}")
    lines.append(
        f"- **Host delta**: {summary['host_delta']} ({summary['host_delta_pct']}%)\n"
    )

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
        f"- **Total var changes**: "
        f"{summary['var_changes_total']} ({summary['var_change_pct']}%)"
    )
    lines.append(f"- **Baseline var keys**: {summary['var_baseline_keys']}\n")

    if summary.get("sample_per_host_changes"):
        lines.append("### Sample per-host changes")
        for host, changes in summary["sample_per_host_changes"].items():
            lines.append(f"- **{host}**")
            if changes["added_keys"]:
                lines.append(
                    f"  - added: {', '.join(f'`{k}`' for k in changes['added_keys'])}"
                )
            if changes["removed_keys"]:
                lines.append(
                    "  - removed: "
                    f"{', '.join(f'`{k}`' for k in changes['removed_keys'])}"
                )
            if changes["changed_values"]:
                lines.append(
                    "  - value changes: "
                    f"{', '.join(f'`{k}`' for k in changes['changed_values'])}"
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
            "- ignored_key_regex: "
            f"{', '.join(f'`{p}`' for p in lim['ignored_key_regex'])}"
        )
    lines.append("")
    return "\n".join(lines)


def _is_scalar(x: Any) -> bool:
    return isinstance(x, (str, int, float, bool)) or x is None


def collect_effective_hostvars(inv_root: YAMLNode | None) -> HostVars:
    """
    Walk the inventory tree (group/host/children) and compute the effective
    host var mapping, merging group vars down to each host.
    """
    hosts: HostVars = {}
    if not inv_root:
        return hosts

    allnode: YAMLNode = inv_root.get("all", inv_root)

    def walk(group_node: YAMLNode, inherited_vars: VarsMap) -> None:
        if not isinstance(group_node, Mapping):
            return

        group_vars_raw = group_node.get("vars")
        group_vars = dict(group_vars_raw) if isinstance(group_vars_raw, Mapping) else {}
        merged = _merge(inherited_vars, group_vars)

        grp_hosts_raw = group_node.get("hosts")
        grp_hosts = dict(grp_hosts_raw) if isinstance(grp_hosts_raw, Mapping) else {}

        for host, hv in grp_hosts.items():
            hv_map = dict(hv) if isinstance(hv, Mapping) else {}
            eff = _merge(merged, hv_map)
            prev = hosts.get(host, {})
            hosts[host] = _merge(prev, eff)

        children_raw = group_node.get("children")
        children = dict(children_raw) if isinstance(children_raw, Mapping) else {}
        for _name, child in children.items():
            if isinstance(child, Mapping):
                walk(child, merged)

    walk(allnode, {})
    return hosts


def filter_vars(d: VarsMap, ignored_regexes: list[re.Pattern[str]]) -> VarsMap:
    """
    Return a copy of vars with keys matching any ignore regex removed.
    """
    if not ignored_regexes:
        return d
    out: VarsMap = {}
    for k, v in d.items():
        if any(rx.search(k) for rx in ignored_regexes):
            continue
        out[k] = v
    return out


def canon(v: Any) -> str:
    try:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        return repr(v)


def normalize_for_compare(
    key: str,
    value: Any,
    setlike_key_patterns: list[re.Pattern[str]],
) -> Any:
    """
    For keys configured as set-like, treat list-of-scalars as unordered:
    deduplicate and sort by canonical form before comparing.
    """
    if isinstance(value, list) and any(p.search(key) for p in setlike_key_patterns):
        if all(_is_scalar(i) for i in value):
            canon_items = sorted({canon(i) for i in value})
            return canon_items
    return value


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
    )
    return merged


# ---------- Main ----------
def main() -> None:
    args_cli = parse_args()
    args = merge_with_config(args_cli)

    ignored_regexes: list[re.Pattern[str]] = [
        re.compile(p) for p in args.ignore_key_regex
    ]
    setlike_key_patterns: list[re.Pattern[str]] = [
        re.compile(p) for p in args.set_like_key_regex
    ]

    current = load_yaml(args.current)
    new = load_yaml(args.new)

    current_hosts = collect_effective_hostvars(current)
    new_hosts = collect_effective_hostvars(new)

    current_host_set: set[str] = set(current_hosts)
    new_host_set: set[str] = set(new_hosts)

    added_hosts: list[str] = sorted(new_host_set - current_host_set)
    removed_hosts: list[str] = sorted(current_host_set - new_host_set)

    host_delta: int = len(added_hosts) + len(removed_hosts)
    current_host_count: int = max(1, len(current_host_set))
    host_delta_pct: float = (host_delta / current_host_count) * 100.0

    common_hosts: list[str] = sorted(current_host_set & new_host_set)
    var_changes: int = 0
    var_baseline_keys: int = 0
    per_host_changes: dict[str, dict[str, list[str]]] = {}

    for h in common_hosts:
        cvars = filter_vars(deepcopy(current_hosts[h]), ignored_regexes)
        nvars = filter_vars(deepcopy(new_hosts[h]), ignored_regexes)

        ckeys: set[str] = set(cvars.keys())
        nkeys: set[str] = set(nvars.keys())

        added_keys: list[str] = sorted(nkeys - ckeys)
        removed_keys: list[str] = sorted(ckeys - nkeys)
        common_keys: set[str] = ckeys & nkeys

        changed_values: list[str] = []
        for k in sorted(common_keys):
            cv = normalize_for_compare(k, cvars.get(k), setlike_key_patterns)
            nv = normalize_for_compare(k, nvars.get(k), setlike_key_patterns)
            if canon(cv) != canon(nv):
                changed_values.append(k)

        changes_for_h: int = len(added_keys) + len(removed_keys) + len(changed_values)
        var_changes += changes_for_h
        var_baseline_keys += len(ckeys)

        if changes_for_h:
            per_host_changes[h] = {
                "added_keys": added_keys,
                "removed_keys": removed_keys,
                "changed_values": changed_values,
            }

    var_baseline_keys = max(1, var_baseline_keys)
    var_change_pct: float = (var_changes / var_baseline_keys) * 100.0

    limits = Limits(
        max_host_change_pct=args.max_host_change_pct,
        max_var_change_pct=args.max_var_change_pct,
        max_host_change_abs=args.max_host_change_abs,
        max_var_change_abs=args.max_var_change_abs,
        ignored_key_regex=args.ignore_key_regex,
    )

    summary = Summary(
        current_hosts=len(current_host_set),
        new_hosts=len(new_host_set),
        host_added=added_hosts,
        host_removed=removed_hosts,
        host_delta=host_delta,
        host_delta_pct=round(host_delta_pct, 3),
        var_changes_total=var_changes,
        var_change_pct=round(var_change_pct, 3),
        var_baseline_keys=var_baseline_keys,
        limits=limits,
        sample_per_host_changes={
            h: per_host_changes[h] for h in list(per_host_changes)[:20]
        },
    )

    # Human-readable + machine-readable
    def _json_default(o: Any):
        if is_dataclass(o):
            return asdict(o)
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    print(
        json.dumps(
            summary,
            default=_json_default,
            indent=2,
            ensure_ascii=False,
        )
    )

    # Optional report + JSON file
    if args.report:
        summary_dict = json.loads(json.dumps(summary, default=_json_default))
        md = _render_markdown(summary_dict)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(
                summary,
                f,
                default=_json_default,
                indent=2,
                ensure_ascii=False,
            )

    def fail(msg: str) -> None:
        print(f"\nSEMANTIC GUARD: {msg}", file=sys.stderr)
        sys.exit(2)

    if host_delta_pct > args.max_host_change_pct:
        fail(
            f"Host delta {host_delta} ({host_delta_pct:.2f}%) "
            f"exceeds limit {args.max_host_change_pct}%."
        )

    if args.max_host_change_abs and host_delta > args.max_host_change_abs:
        fail(
            f"Host delta {host_delta} exceeds absolute cap {args.max_host_change_abs}."
        )

    if var_change_pct > args.max_var_change_pct:
        fail(
            f"Variable changes {var_changes} ({var_change_pct:.2f}%) "
            f"exceed limit {args.max_var_change_pct}%."
        )

    if args.max_var_change_abs and var_changes > args.max_var_change_abs:
        fail(
            f"Variable changes {var_changes} exceed absolute "
            f"cap {args.max_var_change_abs}."
        )

    print("\nSEMANTIC GUARD: change volume OK.")
    sys.exit(0)


if __name__ == "__main__":
    main()
