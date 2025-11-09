import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

# ---------- Fixtures ----------


@pytest.fixture
def base_inventory():
    """Basic inventory with one host."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.0"
        """
    ).lstrip()


@pytest.fixture
def base_inventory_with_vault():
    """Basic inventory with Ansible vault tag."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.0"
              somesecret: !vault |
                $ANSIBLE_VAULT;1.1;AES256
                deadbeefdeadbeef
        """
    ).lstrip()


@pytest.fixture
def inventory_small_change():
    """Inventory with small variable change (version bump)."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.1"
        """
    ).lstrip()


@pytest.fixture
def inventory_small_change_with_vault():
    """Inventory with small change and vault tag."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.1"
              somesecret: !vault |
                $ANSIBLE_VAULT;1.1;AES256
                deadbeefdeadbeef
        """
    ).lstrip()


@pytest.fixture
def inventory_host_added():
    """Inventory with an additional host."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.0"
            app-2:
              app_version: "1.0.0"
        """
    ).lstrip()


@pytest.fixture
def inventory_noisy_key():
    """Inventory with volatile key that should be ignored."""
    return dedent(
        """
        all:
          vars:
            env: prod
          hosts:
            app-1:
              app_version: "1.0.0"
              build_id: "abc123"
        """
    ).lstrip()


@pytest.fixture
def lenient_thresholds():
    """Lenient threshold arguments for tests that should pass."""
    return [
        "--max-host-change-pct",
        "100",
        "--max-var-change-pct",
        "100",
    ]


@pytest.fixture
def strict_thresholds():
    """Strict threshold arguments for tests that should fail."""
    return [
        "--max-host-change-pct",
        "0",
        "--max-var-change-pct",
        "0",
    ]


# ---------- Helper Functions ----------


def run_guard(
    current_yaml: str,
    new_yaml: str,
    tmp_path: Path,
    extra_args=None,
):
    """Write two inventories and run the guard as a subprocess.

    Uses the installed 'inventory-guard' command to test the real entry point.
    """
    current_p = tmp_path / "current.yaml"
    new_p = tmp_path / "new.yaml"
    current_p.write_text(current_yaml, encoding="utf-8")
    new_p.write_text(new_yaml, encoding="utf-8")

    args = [
        "inventory-guard",
        "--current",
        str(current_p),
        "--new",
        str(new_p),
    ]
    if extra_args:
        args += extra_args

    proc = subprocess.run(
        args,
        text=True,
        capture_output=True,
    )
    return proc


def parse_summary(stdout: str) -> dict:
    """
    Extract the JSON blob from stdout.
    With the new output model, JSON is only present when --json flag is used.
    """
    s = stdout.strip()
    if not s:
        return {}
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    return json.loads(s[start : end + 1])


# ---------- Tests ----------


def test_help_succeeds():
    """Test that the installed inventory-guard command shows help."""
    proc = subprocess.run(
        ["inventory-guard", "--help"],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0
    assert "Semantic guard for Ansible inventory changes." in proc.stdout


def test_vault_tag_parses(
    tmp_path, base_inventory_with_vault, inventory_small_change_with_vault
):
    """Test that Ansible vault tags are parsed correctly."""
    proc = run_guard(
        base_inventory_with_vault,
        inventory_small_change_with_vault,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "10",
            "--max-var-change-pct",
            "50",
            "--json",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    summary = parse_summary(proc.stdout)
    assert "var_changes_total" in summary
    assert summary["current_hosts"] == 1
    assert summary["new_hosts"] == 1


def test_small_var_change_passes(tmp_path, base_inventory, inventory_small_change):
    """Test that small variable changes pass with lenient thresholds."""
    proc = run_guard(
        base_inventory,
        inventory_small_change,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "0",
            "--max-var-change-pct",
            "100",
            "--json",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    summary = parse_summary(proc.stdout)
    assert summary["var_changes_total"] >= 1
    assert summary["host_delta"] == 0


def test_host_add_fails_when_threshold_low(
    tmp_path, base_inventory, inventory_host_added, strict_thresholds
):
    """Test that adding hosts fails with strict thresholds."""
    # Allow var changes but not host changes
    args = ["--max-host-change-pct", "0", "--max-var-change-pct", "100"]

    proc = run_guard(
        base_inventory,
        inventory_host_added,
        tmp_path,
        extra_args=args,
    )
    assert proc.returncode == 2
    assert "Host delta" in proc.stderr or "exceeds limit" in proc.stderr


def test_var_change_fails_when_threshold_low(
    tmp_path, base_inventory, inventory_small_change, strict_thresholds
):
    """Test that variable changes fail with strict thresholds."""
    # Allow host changes but not var changes
    args = ["--max-host-change-pct", "100", "--max-var-change-pct", "0"]

    proc = run_guard(
        base_inventory,
        inventory_small_change,
        tmp_path,
        extra_args=args,
    )
    assert proc.returncode == 2
    assert "Variable changes" in proc.stderr or "exceed limit" in proc.stderr


def test_ignore_key_regex_allows_noisy_changes(
    tmp_path, base_inventory, inventory_noisy_key
):
    """Test that ignore-key-regex filters out volatile keys."""
    strict_args = ["--max-host-change-pct", "100", "--max-var-change-pct", "0"]

    # Without ignore, adding 'build_id' should fail
    proc_strict = run_guard(
        base_inventory,
        inventory_noisy_key,
        tmp_path,
        extra_args=strict_args,
    )
    assert proc_strict.returncode == 2

    # With ignore, it should pass
    proc_ignored = run_guard(
        base_inventory,
        inventory_noisy_key,
        tmp_path,
        extra_args=strict_args
        + [
            "--ignore-key-regex",
            "^(build_id)$",
            "--json",
        ],
    )
    assert proc_ignored.returncode == 0, proc_ignored.stderr
    summary = parse_summary(proc_ignored.stdout)
    assert summary["var_changes_total"] == 0


def test_silent_success_by_default(tmp_path, base_inventory):
    """Test that successful runs produce no stdout output by default."""
    proc = run_guard(
        base_inventory,
        base_inventory,  # identical
        tmp_path,
        extra_args=["--max-host-change-pct", "10", "--max-var-change-pct", "10"],
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "Expected empty stdout without --json flag"


def test_json_flag_produces_output(tmp_path, base_inventory):
    """Test that --json flag produces JSON on stdout."""
    proc = run_guard(
        base_inventory,
        base_inventory,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "10",
            "--max-var-change-pct",
            "10",
            "--json",
        ],
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() != "", "Expected JSON output with --json flag"
    summary = parse_summary(proc.stdout)
    assert "current_hosts" in summary


def test_verbose_flag_produces_logs(
    tmp_path, base_inventory, inventory_small_change, lenient_thresholds
):
    """Test that -v flag produces INFO logs on stderr."""
    proc = run_guard(
        base_inventory,
        inventory_small_change,
        tmp_path,
        extra_args=lenient_thresholds + ["-v"],
    )
    assert proc.returncode == 0
    assert "INFO" in proc.stderr or "timestamp" in proc.stderr


def test_debug_flag_produces_detailed_logs(
    tmp_path, base_inventory, inventory_small_change, lenient_thresholds
):
    """Test that -vv flag produces DEBUG logs on stderr."""
    proc = run_guard(
        base_inventory,
        inventory_small_change,
        tmp_path,
        extra_args=lenient_thresholds + ["-vv"],
    )
    assert proc.returncode == 0
    assert "DEBUG" in proc.stderr or "timestamp" in proc.stderr


def test_json_out_writes_file(tmp_path, base_inventory):
    """Test that --json-out writes JSON to a file."""
    json_file = tmp_path / "output.json"

    proc = run_guard(
        base_inventory,
        base_inventory,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "10",
            "--max-var-change-pct",
            "10",
            "--json-out",
            str(json_file),
        ],
    )
    assert proc.returncode == 0
    assert json_file.exists(), "JSON file should be created"

    with open(json_file) as f:
        summary = json.load(f)
    assert "current_hosts" in summary


def test_file_not_found_error(tmp_path, base_inventory):
    """Test proper exit code for missing files."""
    proc = run_guard(
        base_inventory,
        base_inventory,
        tmp_path,
        extra_args=["--current", "/nonexistent/file.yml"],
    )
    assert proc.returncode == 1, "Should exit with code 1 for file errors"
    assert "not found" in proc.stderr.lower() or "error" in proc.stderr.lower()


def test_short_flags_work(tmp_path, base_inventory, inventory_small_change):
    """Test that short flags -c and -n work as alternatives."""
    current_p = tmp_path / "current.yaml"
    new_p = tmp_path / "new.yaml"
    current_p.write_text(base_inventory, encoding="utf-8")
    new_p.write_text(inventory_small_change, encoding="utf-8")

    # Use short flags
    proc = subprocess.run(
        [
            "inventory-guard",
            "-c",
            str(current_p),
            "-n",
            str(new_p),
            "--max-host-change-pct",
            "100",
            "--max-var-change-pct",
            "100",
            "--json",
        ],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    summary = parse_summary(proc.stdout)
    assert summary["var_changes_total"] >= 1
