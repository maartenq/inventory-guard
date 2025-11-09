import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
SCRIPT = REPO_ROOT / "src" / "inventory_guard" / "main.py"


def run_guard(
    current_yaml: str,
    new_yaml: str,
    tmp_path: Path,
    extra_args=None,
):
    """Write two inventories and run the guard as a subprocess."""
    current_p = tmp_path / "current.yaml"
    new_p = tmp_path / "new.yaml"
    current_p.write_text(current_yaml, encoding="utf-8")
    new_p.write_text(new_yaml, encoding="utf-8")

    args = [
        sys.executable,
        str(SCRIPT),
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
        cwd=str(REPO_ROOT),
    )
    return proc


def parse_summary(stdout: str) -> dict:
    """
    Extract the JSON blob from stdout. The script prints a JSON summary
    followed by a status line ("SEMANTIC GUARD: ...").
    """
    s = stdout.strip()
    start = s.find("{")
    end = s.rfind("}")
    assert start != -1 and end != -1 and end > start, (
        f"Did not find JSON in output:\n{s}"
    )
    return json.loads(s[start : end + 1])


def base_inventory(with_vault: bool = False) -> str:
    if with_vault:
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
    else:
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


def inventory_with_small_changes(with_vault: bool = False) -> str:
    """
    Return an inventory similar to base_inventory(), but with a small change
    (e.g., app_version bumped). Optionally includes a !vault secret block.
    """
    if with_vault:
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
    else:
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


def inventory_with_host_add() -> str:
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


def inventory_with_noisy_key_change() -> str:
    # only a volatile key changes; we will ignore it via --ignore-key-regex
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


def test_help_succeeds():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    assert "Semantic guard for Ansible inventory changes." in proc.stdout


def test_vault_tag_parses(tmp_path):
    current_yaml = base_inventory(with_vault=True)
    new_yaml = inventory_with_small_changes(with_vault=True)
    proc = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "10",
            "--max-var-change-pct",
            "50",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    summary = parse_summary(proc.stdout)
    assert "var_changes_total" in summary
    assert summary["current_hosts"] == 1
    assert summary["new_hosts"] == 1


def test_small_var_change_passes(tmp_path):
    current_yaml = base_inventory()
    new_yaml = inventory_with_small_changes()
    proc = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "0",  # no host churn allowed
            "--max-var-change-pct",
            "100",  # 1 change / 2 baseline keys = 50%; allow it
        ],
    )
    assert proc.returncode == 0, proc.stderr
    summary = parse_summary(proc.stdout)
    assert summary["var_changes_total"] >= 1
    assert summary["host_delta"] == 0


def test_host_add_fails_when_threshold_low(tmp_path):
    current_yaml = base_inventory()
    new_yaml = inventory_with_host_add()
    proc = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "0",
            "--max-var-change-pct",
            "100",
        ],
    )
    assert proc.returncode == 2
    assert "Host delta" in proc.stderr
    assert "exceeds limit" in proc.stderr


def test_var_change_fails_when_threshold_low(tmp_path):
    current_yaml = base_inventory()
    new_yaml = inventory_with_small_changes()
    proc = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "100",
            "--max-var-change-pct",
            "0",
        ],
    )
    assert proc.returncode == 2
    assert "Variable changes" in proc.stderr
    assert "exceed limit" in proc.stderr


def test_ignore_key_regex_allows_noisy_changes(tmp_path):
    current_yaml = base_inventory()
    new_yaml = inventory_with_noisy_key_change()
    # Without ignore, adding 'build_id' would count as a var add/change
    proc_strict = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "100",
            "--max-var-change-pct",
            "0",
        ],
    )
    assert proc_strict.returncode == 2  # should fail strictly

    # With ignore, it should pass
    proc_ignored = run_guard(
        current_yaml,
        new_yaml,
        tmp_path,
        extra_args=[
            "--max-host-change-pct",
            "100",
            "--max-var-change-pct",
            "0",
            "--ignore-key-regex",
            "^(build_id)$",
        ],
    )
    assert proc_ignored.returncode == 0, proc_ignored.stderr
    summary = parse_summary(proc_ignored.stdout)
    assert summary["var_changes_total"] == 0
