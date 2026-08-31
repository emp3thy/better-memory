"""`better-memory setup` and `better-memory doctor` subcommands."""
from __future__ import annotations

import json
from pathlib import Path

from better_memory._common import resolve_home
from better_memory.setup import engine, manifest


def add_setup_parser(subparsers) -> None:
    subparsers.add_parser("setup", help="Install/repair all Claude Code wiring.")


def add_doctor_parser(subparsers) -> None:
    p = subparsers.add_parser("doctor", help="Check wiring drift; --fix repairs.")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")


def _home_layout(home: Path) -> None:
    for sub in ("spool", "state", "install-backups",
                "knowledge-base/standards", "knowledge-base/languages",
                "knowledge-base/projects"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    if not settings.exists():
        settings.write_text('{"storage_backend": "sqlite"}\n', encoding="utf-8")


def handle_setup(args) -> int:
    params = manifest.detect_machine_params(home=str(resolve_home()))
    home = Path(params.home)
    _home_layout(home)
    paths = engine.default_target_paths()
    report = engine.apply(params, paths, home=home)
    for line in report.repaired:
        print(f"  OK {line}")
    for line in report.warnings:
        print(f"  WARN {line}")
    print("[better-memory setup] Done. Restart Claude Code to load changes.")
    return 0


def handle_doctor(args) -> int:
    params = manifest.detect_machine_params(home=str(resolve_home()))
    paths = engine.default_target_paths()
    if args.fix:
        home = Path(params.home)
        report = engine.apply(params, paths, home=home)
        for line in report.repaired:
            print(f"  FIXED {line}")
        for line in report.warnings:
            print(f"  WARN {line}")
        return 0
    drift = engine.diff(params, paths)
    if args.as_json:
        print(json.dumps({"drift": drift}))
    elif drift:
        print(f"[better-memory doctor] {len(drift)} drift item(s):")
        for line in drift:
            print(f"  DRIFT {line}")
    else:
        print("[better-memory doctor] wiring clean")
    return 1 if drift else 0
