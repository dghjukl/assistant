"""
EOS — Release Gate
==================
Machine-checkable invariants that must all pass before any release that
touches autonomy-, safety-, or control-plane-sensitive code.

Run: python release_gate.py

Exit codes:
    0 — all required gates passed (warnings may still be present)
    1 — one or more required gates failed

Gates
-----
1.  Safety defaults: autonomy_defaults.action/initiative/computer_use are OFF
    in every shipped config profile (hardened, base, standard, full, vision).
2.  Hardened profile exists and is valid JSON with required locked-down keys.
3.  Critical safety-plane source files exist:
    auth.py, audit.py, secrets.py, db_migrations.py, google_oauth.py,
    survival_mode.py, tool_executor.py, tool_registry.py.
4.  No plaintext credential files are tracked by git.
5.  pydantic is listed in requirements.txt (typed contract dependency).
6.  Config credential key normalization: no config file uses the old
    client_secret_glob key.
7.  Survival mode imports cleanly and activates/deactivates without error.
8.  Test suite runs and passes (pytest exit code 0).

Gates 1-7 are fast static checks.  Gate 8 runs the full test suite and may
take longer.  Pass --skip-tests to run only the static checks.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# ── Formatting ────────────────────────────────────────────────────────────────

CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def ok(msg: str)   -> None: print(f"  {GREEN}[PASS]   {RESET}{msg}")
def warn(msg: str) -> None: print(f"  {YELLOW}[WARN]   {RESET}{msg}")
def fail(msg: str) -> None: print(f"  {RED}[FAIL]   {RESET}{msg}")
def info(msg: str) -> None: print(f"           {CYAN}{msg}{RESET}")
def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * max(0, 52 - len(title))}{RESET}")


_failures: list[str] = []
_warnings: list[str] = []


def require(passed: bool, label: str, fix: str = "") -> None:
    if passed:
        ok(label)
    else:
        fail(label)
        if fix:
            info(f"Fix: {fix}")
        _failures.append(label)


def advisory(passed: bool, label: str, fix: str = "") -> None:
    if passed:
        ok(label)
    else:
        warn(label)
        if fix:
            info(f"Fix: {fix}")
        _warnings.append(label)


# ── Gate 1: Safety defaults ───────────────────────────────────────────────────

def check_safety_defaults() -> None:
    section("Safety Defaults in Config Profiles")

    # All shipped profiles that will be installed/distributed
    profiles = [
        "config.hardened.json",
        "config.base.json",
        "config.base-thinking.json",
        "config.base-creativity.json",
        "config.standard.json",
        "config.full.json",
        "config.vision.json",
    ]

    for name in profiles:
        path = ROOT / name
        if not path.is_file():
            advisory(False, f"{name} — not found (skipping safety check)",
                     f"Create {name} or remove it from the profile list")
            continue

        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            require(False, f"{name} — invalid JSON: {exc}")
            continue

        ad = cfg.get("autonomy_defaults", {})

        require(
            ad.get("action", False) is False,
            f"{name}: autonomy_defaults.action = false",
            f"Set autonomy_defaults.action to false in {name}",
        )
        require(
            ad.get("initiative", False) is False,
            f"{name}: autonomy_defaults.initiative = false",
            f"Set autonomy_defaults.initiative to false in {name}",
        )

        cu = cfg.get("computer_use", {})
        require(
            cu.get("enabled", False) is False,
            f"{name}: computer_use.enabled = false",
            f"Set computer_use.enabled to false in {name}",
        )
        require(
            cu.get("default_mode", "off") == "off",
            f"{name}: computer_use.default_mode = 'off'",
            f"Set computer_use.default_mode to 'off' in {name}",
        )


# ── Gate 2: Hardened profile ──────────────────────────────────────────────────

def check_hardened_profile() -> None:
    section("Hardened Release Profile")

    path = ROOT / "config.hardened.json"
    require(path.is_file(), "config.hardened.json exists",
            "Run: create config.hardened.json (see config.base.json as template)")

    if not path.is_file():
        return

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        require(False, f"config.hardened.json is valid JSON: {exc}")
        return

    ok("config.hardened.json is valid JSON")

    # Hardened profile must disable all external integrations
    require(
        cfg.get("discord", {}).get("enabled", True) is False,
        "config.hardened.json: discord.enabled = false",
    )
    require(
        cfg.get("google", {}).get("enabled", True) is False,
        "config.hardened.json: google.enabled = false",
    )

    # Only safe toolpacks
    packs = set(cfg.get("toolpacks", {}).get("packs", []))
    dangerous = {"privileged_tools", "process_tools", "service_control_tools",
                 "system_cmd_tools", "scheduler_tools", "recovery_tools"}
    loaded_dangerous = packs & dangerous
    require(
        not loaded_dangerous,
        "config.hardened.json: no privileged/dangerous toolpacks loaded",
        f"Remove from packs: {', '.join(sorted(loaded_dangerous))}",
    )

    advisory(
        cfg.get("workspace_tools", {}).get("allow_exec", True) is False,
        "config.hardened.json: workspace_tools.allow_exec = false",
    )
    advisory(
        cfg.get("workspace_tools", {}).get("allow_delete", True) is False,
        "config.hardened.json: workspace_tools.allow_delete = false",
    )


# ── Gate 3: Critical source files ────────────────────────────────────────────

def check_critical_source_files() -> None:
    section("Critical Safety-Plane Source Files")

    required_files = [
        ("core/auth.py",            "Admin authentication middleware"),
        ("core/audit.py",           "Durable audit log store"),
        ("core/secrets.py",         "Secure credential manager"),
        ("core/db_migrations.py",   "Schema versioning and migrations"),
        ("core/google_oauth.py",    "Google OAuth flow manager"),
        ("runtime/survival_mode.py","Last-ditch fallback mode"),
        ("runtime/tool_executor.py","Governed tool execution engine"),
        ("runtime/tool_registry.py","Single-source tool catalog"),
        ("webui/schemas.py",        "Typed request/response contracts"),
    ]

    for rel, label in required_files:
        p = ROOT / rel
        require(p.is_file(), f"{rel} — {label}",
                f"File is missing: {ROOT / rel}")


# ── Gate 4: No plaintext credentials in git ───────────────────────────────────

def check_no_plaintext_credentials() -> None:
    section("No Plaintext Credentials Tracked by Git")

    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True, text=True, timeout=10,
        )
        tracked = result.stdout.splitlines()
    except Exception as exc:
        advisory(False, f"Could not query git tracked files: {exc}")
        return

    credential_patterns = [
        "client_secret",   # Google OAuth client secrets
        "Discord.txt",     # Discord bot tokens
        ".env",            # Environment variable files
        "admin_token.txt", # Admin tokens
        "google_token.json", # OAuth refresh tokens
    ]

    found_creds = []
    for f in tracked:
        for pat in credential_patterns:
            if pat.lower() in f.lower():
                found_creds.append(f)
                break

    require(
        not found_creds,
        "No plaintext credential files tracked by git",
        "Add these files to .gitignore and remove from git index: "
        + ", ".join(found_creds),
    )
    if found_creds:
        for f in found_creds:
            info(f"  Found: {f}")


# ── Gate 5: requirements.txt completeness ────────────────────────────────────

def check_requirements_completeness() -> None:
    section("Requirements Completeness")

    req_path = ROOT / "requirements.txt"
    require(req_path.is_file(), "requirements.txt exists")
    if not req_path.is_file():
        return

    content = req_path.read_text(encoding="utf-8").lower()

    critical = {
        "pydantic":   "typed WebUI request/response contracts",
        "fastapi":    "web framework",
        "keyring":    "secrets manager backend",
        "jsonschema": "tool parameter validation",
    }

    for pkg, reason in critical.items():
        require(
            pkg in content,
            f"requirements.txt contains {pkg} ({reason})",
            f"Add {pkg} to requirements.txt",
        )


# ── Gate 6: Config key normalization ─────────────────────────────────────────

def check_config_key_normalization() -> None:
    section("Config Key Normalization")

    config_files = list(ROOT.glob("config*.json"))
    stale_key_found = []

    for p in config_files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "client_secret_glob" in text:
            stale_key_found.append(p.name)

    require(
        not stale_key_found,
        "No config files use deprecated client_secret_glob key",
        "Rename to client_secret_path in: " + ", ".join(stale_key_found),
    )

    # Check that duplicate google_tools toolpack sub-config is gone
    dup_found = []
    for p in config_files:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt = cfg.get("toolpacks", {}).get("google_tools", {})
        if "client_secret" in str(gt) or "token_path" in str(gt):
            dup_found.append(p.name)

    require(
        not dup_found,
        "No config files have duplicate Google credential keys in toolpacks.google_tools",
        "Remove client_secret_* and token_path from toolpacks.google_tools in: "
        + ", ".join(dup_found),
    )


# ── Gate 7: Survival mode self-test ──────────────────────────────────────────

def check_survival_mode() -> None:
    section("Survival Mode Self-Test")

    try:
        sys.path.insert(0, str(ROOT))
        from runtime.survival_mode import SurvivalModeService, SurvivalReason

        svc = SurvivalModeService()
        assert not svc.is_active, "Should be inactive on creation"

        svc.activate(SurvivalReason.PRIMARY_BOOT_FAILURE, detail="gate test")
        assert svc.is_active, "Should be active after activate()"

        resp = svc.handle_turn("/help")
        assert resp and "survival" in resp.lower(), "Help response must mention survival"

        resp = svc.handle_turn("/status")
        assert "primary_boot_failure" in resp, "Status must include reason"

        resp = svc.handle_turn("/diagnose")
        assert "gate test" in resp, "Diagnose must include detail"

        svc.deactivate()
        assert not svc.is_active, "Should be inactive after deactivate()"

        ok("survival_mode.py imports, activates, handles turns, deactivates")
    except Exception as exc:
        require(False, f"Survival mode self-test: {exc}",
                "Fix runtime/survival_mode.py")


# ── Gate 8: Test suite ────────────────────────────────────────────────────────

def check_test_suite() -> None:
    section("Test Suite")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(ROOT / "tests"),
             "-v", "--tb=short", "-q"],
            capture_output=False,
            cwd=str(ROOT),
            timeout=300,
        )
        require(
            result.returncode == 0,
            "pytest exits 0 (all tests pass)",
            "Fix failing tests before release",
        )
    except subprocess.TimeoutExpired:
        require(False, "pytest timed out (>300s)",
                "Tests are too slow or hung — investigate")
    except FileNotFoundError:
        advisory(False, "pytest not found — skipping test suite check",
                 "Install test dependencies: pip install pytest pytest-asyncio")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EOS Release Gate")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Skip the test suite (run static checks only)")
    args = parser.parse_args()

    print(f"\n{BOLD}{CYAN}EOS Release Gate{'═' * 40}{RESET}")
    print(f"Root: {ROOT}\n")

    check_safety_defaults()
    check_hardened_profile()
    check_critical_source_files()
    check_no_plaintext_credentials()
    check_requirements_completeness()
    check_config_key_normalization()
    check_survival_mode()

    if not args.skip_tests:
        check_test_suite()
    else:
        print(f"\n  {YELLOW}[SKIP] Test suite skipped (--skip-tests){RESET}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═' * 56}")

    if _failures:
        print(f"\n{RED}{BOLD}  RELEASE BLOCKED — {len(_failures)} gate(s) failed:{RESET}")
        for f in _failures:
            print(f"  {RED}• {f}{RESET}")
        if _warnings:
            print(f"\n{YELLOW}  {len(_warnings)} advisory warning(s) also present.{RESET}")
        print(f"\n  {RED}Fix all failures before tagging a release.{RESET}\n")
        sys.exit(1)
    else:
        print(f"\n{GREEN}{BOLD}  All release gates passed.{RESET}")
        if _warnings:
            print(f"{YELLOW}  {len(_warnings)} advisory warning(s) — review before release.{RESET}")
            for w in _warnings:
                print(f"  {YELLOW}• {w}{RESET}")
        print(f"\n  {GREEN}Safe to tag and release.{RESET}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
