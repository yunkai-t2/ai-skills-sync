from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import importlib.resources
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "endpoints": [
        {"name": "claude", "path": "~/.claude/skills", "write": True, "exclude": [".system"]},
        {"name": "cursor", "path": "~/.cursor/skills", "write": True},
        {"name": "copilot", "path": "~/.copilot/skills", "write": True, "exclude": [".system"]},
        {
            "name": "codex",
            "path": "~/.codex/skills",
            "write": True,
            "exclude": [".system"],
        },
    ],
    "ignored_patterns": [
        ".DS_Store",
        "Thumbs.db",
        "__pycache__",
        "*.pyc",
        ".sync-manifest.json",
        ".cursor-managed-skills-manifest.json",
    ],
    "state_path": "~/.local/state/ai-skills-sync/state.json",
}


@dataclasses.dataclass(frozen=True)
class Endpoint:
    name: str
    path: Path
    write: bool
    exclude: frozenset[str]


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    endpoint: str
    path: Path
    digest: str


@dataclasses.dataclass(frozen=True)
class Action:
    skill: str
    source: Skill
    target: Endpoint
    reason: str


@dataclasses.dataclass(frozen=True)
class Conflict:
    skill: str
    variants: dict[str, str]
    message: str


def default_config_path() -> Path:
    value = os.environ.get("AI_SKILLS_SYNC_CONFIG")
    if value:
        return Path(value).expanduser()
    return Path("~/.config/ai-skills-sync/config.json").expanduser()


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def parse_endpoints(config: dict[str, Any]) -> list[Endpoint]:
    raw_endpoints = config.get("endpoints")
    if not isinstance(raw_endpoints, list) or not raw_endpoints:
        raise SystemExit("config must contain a non-empty endpoints list")

    endpoints: list[Endpoint] = []
    seen: set[str] = set()
    for item in raw_endpoints:
        if not isinstance(item, dict):
            raise SystemExit("each endpoint must be an object")
        name = item.get("name")
        path = item.get("path")
        if not isinstance(name, str) or not name:
            raise SystemExit("each endpoint needs a non-empty name")
        if name in seen:
            raise SystemExit(f"duplicate endpoint name: {name}")
        if not isinstance(path, str) or not path:
            raise SystemExit(f"endpoint {name} needs a non-empty path")
        exclude = item.get("exclude", [])
        if not isinstance(exclude, list) or not all(
            isinstance(value, str) for value in exclude
        ):
            raise SystemExit(f"endpoint {name} exclude must be a list of strings")
        endpoints.append(
            Endpoint(
                name=name,
                path=Path(path).expanduser(),
                write=bool(item.get("write", True)),
                exclude=frozenset(exclude),
            )
        )
        seen.add(name)
    return endpoints


def ignored_patterns(config: dict[str, Any]) -> list[str]:
    patterns = config.get("ignored_patterns", [])
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) for pattern in patterns
    ):
        raise SystemExit("ignored_patterns must be a list of strings")
    return patterns


def should_ignore(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def hash_skill_dir(path: Path, patterns: list[str]) -> str:
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(name for name in dirs if not should_ignore(name, patterns))
        for filename in sorted(files):
            if should_ignore(filename, patterns):
                continue
            file_path = Path(root) / filename
            rel = file_path.relative_to(path).as_posix()
            try:
                if file_path.is_symlink():
                    digest.update(b"L\0")
                    digest.update(rel.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(os.readlink(file_path).encode("utf-8"))
                    digest.update(b"\0")
                elif file_path.is_file():
                    digest.update(b"F\0")
                    digest.update(rel.encode("utf-8"))
                    digest.update(b"\0")
                    with file_path.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            digest.update(chunk)
                    digest.update(b"\0")
            except FileNotFoundError:
                continue
    return digest.hexdigest()


def scan_endpoint(endpoint: Endpoint, patterns: list[str]) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not endpoint.path.exists():
        return skills
    if not endpoint.path.is_dir():
        raise SystemExit(f"endpoint path is not a directory: {endpoint.path}")

    for child in sorted(endpoint.path.iterdir(), key=lambda item: item.name):
        if child.name in endpoint.exclude:
            continue
        if not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            continue
        skills[child.name] = Skill(
            name=child.name,
            endpoint=endpoint.name,
            path=child,
            digest=hash_skill_dir(child, patterns),
        )
    return skills


def scan_all(
    endpoints: list[Endpoint], patterns: list[str]
) -> dict[str, dict[str, Skill]]:
    return {
        endpoint.name: scan_endpoint(endpoint, patterns)
        for endpoint in endpoints
    }


def previous_hash(state: dict[str, Any], skill: str, endpoint: str) -> str | None:
    value = (
        state.get("skills", {})
        .get(skill, {})
        .get("tools", {})
        .get(endpoint, {})
        .get("hash")
    )
    return value if isinstance(value, str) else None


def first_endpoint_name(endpoints: list[Endpoint], names: set[str]) -> str:
    for endpoint in endpoints:
        if endpoint.name in names:
            return endpoint.name
    return sorted(names)[0]


def choose_source(
    skill: str,
    present: dict[str, Skill],
    state: dict[str, Any],
    endpoints: list[Endpoint],
    prefer: str | None,
) -> tuple[str | None, Conflict | None]:
    if prefer is not None:
        if prefer not in present:
            return (
                None,
                Conflict(
                    skill=skill,
                    variants={name: item.digest[:12] for name, item in present.items()},
                    message=f"--prefer {prefer} was requested, but that endpoint has no skill named {skill}",
                ),
            )
        return prefer, None

    hashes = {item.digest for item in present.values()}
    if len(hashes) == 1:
        return first_endpoint_name(endpoints, set(present)), None

    changed = [
        name
        for name, item in present.items()
        if previous_hash(state, skill, name) != item.digest
    ]
    if len(changed) == 1:
        return changed[0], None

    return (
        None,
        Conflict(
            skill=skill,
            variants={name: item.digest[:12] for name, item in present.items()},
            message="multiple different copies exist; rerun with --prefer ENDPOINT after choosing the winner",
        ),
    )


def make_plan(
    endpoints: list[Endpoint],
    inventory: dict[str, dict[str, Skill]],
    state: dict[str, Any],
    prefer: str | None,
) -> tuple[list[Action], list[Conflict]]:
    endpoint_by_name = {endpoint.name: endpoint for endpoint in endpoints}
    if prefer is not None and prefer not in endpoint_by_name:
        raise SystemExit(f"unknown endpoint for --prefer: {prefer}")

    skill_names = sorted(
        {skill_name for skills in inventory.values() for skill_name in skills}
    )
    actions: list[Action] = []
    conflicts: list[Conflict] = []

    for skill_name in skill_names:
        present = {
            endpoint_name: skills[skill_name]
            for endpoint_name, skills in inventory.items()
            if skill_name in skills
        }
        source_name, conflict = choose_source(
            skill_name, present, state, endpoints, prefer
        )
        if conflict is not None or source_name is None:
            conflicts.append(conflict or Conflict(skill_name, {}, "unknown conflict"))
            continue

        source = present[source_name]
        for endpoint in endpoints:
            if not endpoint.write:
                continue
            current = present.get(endpoint.name)
            if current is not None and current.digest == source.digest:
                continue
            reason = "missing" if current is None else "outdated"
            if prefer is not None:
                reason = f"{reason}, preferred {prefer}"
            actions.append(
                Action(
                    skill=skill_name,
                    source=source,
                    target=endpoint,
                    reason=reason,
                )
            )

    return actions, conflicts


def remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def copy_ignore(patterns: list[str]):
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if should_ignore(name, patterns)}

    return ignore


def copy_skill(source: Skill, target_endpoint: Endpoint, patterns: list[str]) -> None:
    target_root = target_endpoint.path
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / source.name
    tmp = target_root / f".{source.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"

    try:
        if tmp.exists() or tmp.is_symlink():
            remove_existing(tmp)
        shutil.copytree(
            source.path,
            tmp,
            symlinks=True,
            ignore=copy_ignore(patterns),
            copy_function=shutil.copy2,
        )
        remove_existing(target)
        os.replace(tmp, target)
    except Exception:
        try:
            remove_existing(tmp)
        except FileNotFoundError:
            pass
        raise


def state_from_inventory(
    inventory: dict[str, dict[str, Skill]], endpoints: list[Endpoint]
) -> dict[str, Any]:
    skills: dict[str, Any] = {}
    for endpoint in endpoints:
        for skill_name, skill in inventory.get(endpoint.name, {}).items():
            entry = skills.setdefault(skill_name, {"tools": {}})
            entry["tools"][endpoint.name] = {
                "hash": skill.digest,
                "path": str(skill.path),
            }
    return {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skills": skills,
    }


def print_inventory(
    inventory: dict[str, dict[str, Skill]], endpoints: list[Endpoint]
) -> None:
    for endpoint in endpoints:
        skills = inventory.get(endpoint.name, {})
        print(f"{endpoint.name}: {endpoint.path}")
        if not skills:
            print("  (no skills)")
            continue
        for name in sorted(skills):
            print(f"  {name}  {skills[name].digest[:12]}")


def print_plan(actions: list[Action], conflicts: list[Conflict]) -> None:
    for conflict in conflicts:
        variants = ", ".join(
            f"{endpoint}={digest}" for endpoint, digest in sorted(conflict.variants.items())
        )
        suffix = f" ({variants})" if variants else ""
        print(f"conflict {conflict.skill}: {conflict.message}{suffix}")
    for action in actions:
        print(
            "copy "
            f"{action.skill}: {action.source.endpoint} -> {action.target.name} "
            f"({action.reason})"
        )
    if not actions and not conflicts:
        print("ok: no changes")


def cmd_install_systemd(enable: bool) -> int:
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)

    data_pkg = importlib.resources.files("ai_skills_sync") / "data"
    for unit_name in ("ai-skills-sync.service", "ai-skills-sync.timer"):
        src = data_pkg / unit_name
        dst = unit_dir / unit_name
        dst.write_bytes(src.read_bytes())
        print(f"installed {dst}")

    print()
    print("Run the following to activate the timer:")
    print("  systemctl --user daemon-reload")
    if enable:
        print("  systemctl --user enable --now ai-skills-sync.timer")
    else:
        print("  systemctl --user enable --now ai-skills-sync.timer  # optional")

    if enable:
        import subprocess
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "ai-skills-sync.timer"],
            check=True,
        )
        print("timer enabled")

    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Sync personal SKILL.md directories across Claude, Cursor, Copilot, and Codex."
    )
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="config file path (default: ~/.config/ai-skills-sync/config.json)",
    )
    parser.add_argument(
        "--write-default-config",
        action="store_true",
        help="write the default config to --config and exit",
    )
    parser.add_argument("--dry-run", action="store_true", help="show planned changes")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when changes or conflicts are pending",
    )
    parser.add_argument("--list", action="store_true", help="show current inventory")
    parser.add_argument(
        "--prefer",
        help="resolve same-name conflicts by copying this endpoint's version",
    )
    parser.add_argument(
        "--install-systemd",
        action="store_true",
        help="install systemd service and timer units to ~/.config/systemd/user/",
    )
    parser.add_argument(
        "--enable-timer",
        action="store_true",
        help="with --install-systemd: also run daemon-reload and enable the timer",
    )
    args = parser.parse_args(argv)

    if args.install_systemd:
        return cmd_install_systemd(enable=args.enable_timer)

    config_path = Path(args.config).expanduser()
    if args.write_default_config:
        write_json(config_path, DEFAULT_CONFIG)
        print(f"wrote {config_path}")
        return 0

    config = load_json(config_path, DEFAULT_CONFIG)
    endpoints = parse_endpoints(config)
    patterns = ignored_patterns(config)
    state_path = Path(config.get("state_path", DEFAULT_CONFIG["state_path"])).expanduser()
    state = load_json(state_path, {"version": 1, "skills": {}})

    inventory = scan_all(endpoints, patterns)
    if args.list:
        print_inventory(inventory, endpoints)
        return 0

    actions, conflicts = make_plan(endpoints, inventory, state, args.prefer)
    print_plan(actions, conflicts)

    if conflicts:
        return 2
    if args.check:
        return 1 if actions else 0
    if args.dry_run:
        return 0

    for action in actions:
        copy_skill(action.source, action.target, patterns)

    refreshed = scan_all(endpoints, patterns)
    write_json(state_path, state_from_inventory(refreshed, endpoints))
    return 0


def cli_main() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli_main()
