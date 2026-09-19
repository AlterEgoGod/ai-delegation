"""Install a pinned local ai-delegation checkout. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

BEGIN = '<!-- ai-delegation:begin -->'
END = '<!-- ai-delegation:end -->'


def git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise ValueError('Git failed: ' + result.stderr.strip())
    return result.stdout.strip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes project: ' + relative)
    # Reject symlinks/junctions even when they currently resolve inside the project.
    current = path
    while current != root:
        if current.is_symlink() or (current.exists() and current.resolve() != current.absolute()):
            raise ValueError('Linked path is not supported: ' + relative)
        current = current.parent
    return path


def read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8-sig') if path.exists() else ''


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return json.dumps(value, ensure_ascii=False)


def merge_config(existing: str, desired: dict) -> str:
    """Add missing settings; conflicts fail before any writes. Preserve existing text."""
    current = tomllib.loads(existing)
    missing_root = {}
    missing_agents = {}
    for key, value in desired.items():
        if key == 'agents':
            continue
        if key in current and (type(current[key]) is not type(value) or current[key] != value):
            raise ValueError('Conflicting config key: ' + key + '; resolve it explicitly before installing')
        if key not in current:
            missing_root[key] = value
    agents = current.get('agents', {})
    if not isinstance(agents, dict):
        raise ValueError('agents must be a TOML table')
    if 'max_threads' in agents:
        raise ValueError('Legacy agents.max_threads exists; migrate it explicitly first')
    for key, value in desired['agents'].items():
        if key in agents and (type(agents[key]) is not type(value) or agents[key] != value):
            raise ValueError('Conflicting config key: agents.' + key + '; resolve it explicitly before installing')
        if key not in agents:
            missing_agents[key] = value
    prefix = ''.join(f'{key} = {toml_value(value)}\n' for key, value in missing_root.items())
    result = prefix + existing
    if missing_agents:
        additions = ''.join(f'{key} = {toml_value(value)}\n' for key, value in missing_agents.items())
        if 'agents' not in current:
            result = result.rstrip() + '\n\n[agents]\n' + additions
        else:
            headers = list(re.finditer(r'(?m)^\s*\[agents\][ \t]*(?:#[^\n]*)?\r?$', result))
            if len(headers) != 1:
                raise ValueError('Use a plain [agents] table before merging missing agent settings')
            offset = headers[0].end()
            result = result[:offset] + '\n' + additions + result[offset:]
    parsed = tomllib.loads(result)
    expected = dict(current)
    expected.update({k: v for k, v in desired.items() if k != 'agents'})
    expected['agents'] = {**agents, **desired['agents']}
    if parsed != expected:
        raise ValueError('Config merge changed unrelated values')
    return result


def build_plan(source: Path, project: Path) -> tuple[dict[Path, bytes], str]:
    source, project = source.resolve(), project.resolve()
    if Path(git(project, 'rev-parse', '--show-toplevel')).resolve() != project:
        raise ValueError('--project must be the root of a Git project')
    if source == project or not source.is_relative_to(project):
        raise ValueError('Clone/add the module inside the target project first')
    relative = source.relative_to(project).as_posix()
    safe_path(project, relative)
    if any(c in relative for c in '\n\r`'):
        raise ValueError('Unsupported module path characters')
    if Path(git(source, 'rev-parse', '--show-toplevel')).resolve() != source:
        raise ValueError('Module must be its own Git checkout or submodule')
    if git(source, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Module has uncommitted changes; install a committed version')
    commit = git(source, 'rev-parse', 'HEAD')
    instruction_root = source / '99_Инструкции'
    desired = tomllib.loads(read_text(instruction_root / '02_Конфигурация/config.toml'))
    config_path = safe_path(project, '.codex/config.toml')
    merged = merge_config(read_text(config_path), desired)
    if safe_path(project, 'AGENTS.override.md').exists():
        raise ValueError('AGENTS.override.md takes precedence; integrate it explicitly first')
    lock_path = safe_path(project, '.codex/ai-delegation.lock.json')
    old_lock = json.loads(read_text(lock_path)) if lock_path.exists() else {}
    if old_lock and (old_lock.get('schema') != 1 or old_lock.get('source') != relative):
        raise ValueError('Existing module installation uses a different source or schema')
    plan = {config_path: merged.encode('utf-8')}
    role_hashes = {}
    role_names = set()
    for template in sorted((instruction_root / '02_Конфигурация').glob('delegation-*-worker.toml')):
        data = template.read_bytes()
        parsed = tomllib.loads(data.decode('utf-8-sig'))
        if not all(parsed.get(key) for key in ('name', 'description', 'developer_instructions', 'model')):
            raise ValueError('Incomplete role: ' + template.name)
        target_relative = '.codex/agents/' + template.name
        target = safe_path(project, target_relative)
        if target.exists() and target.read_bytes() != data:
            previous_hash = old_lock.get('roles', {}).get(target_relative)
            if not previous_hash or digest(target.read_bytes()) != previous_hash:
                raise ValueError('Role has existing/user changes: ' + target_relative)
        plan[target] = data
        role_hashes[target_relative] = digest(data)
        role_names.add(parsed['name'])
    if len(role_hashes) != 3:
        raise ValueError('Expected three worker configurations')
    agents_dir = safe_path(project, '.codex/agents')
    for existing in agents_dir.glob('*.toml'):
        safe_path(project, existing.relative_to(project).as_posix())
        if existing in plan:
            continue
        if tomllib.loads(read_text(existing)).get('name') in role_names:
            raise ValueError('Duplicate custom agent name in ' + existing.name)
    agents_path = safe_path(project, 'AGENTS.md')
    existing_agents = read_text(agents_path)
    block = (BEGIN + '\n' + 'Перед работой прочитай `' + relative
             + '/99_Инструкции/RULES.md` и применяй правила выбора исполнителей и приёмки.\n'
             + 'Версия модуля закреплена в `.codex/ai-delegation.lock.json`. Не обновляй её автоматически.\n' + END)
    if BEGIN in existing_agents or END in existing_agents:
        if existing_agents.count(BEGIN) != 1 or existing_agents.count(END) != 1:
            raise ValueError('Malformed ai-delegation block in AGENTS.md')
        start, end = existing_agents.index(BEGIN), existing_agents.index(END)
        if start > end:
            raise ValueError('Malformed ai-delegation block order')
        new_agents = existing_agents[:start] + block + existing_agents[end + len(END):]
    else:
        new_agents = existing_agents + ('\n\n' if existing_agents else '') + block + '\n'
    plan[agents_path] = new_agents.encode('utf-8')
    source_hashes = {}
    for item in sorted(instruction_root.rglob('*')):
        if item.is_file() and '99_Архив' not in item.parts and '__pycache__' not in item.parts:
            source_hashes[item.relative_to(source).as_posix()] = digest(item.read_bytes())
    lock = {'schema': 1, 'repository': 'https://github.com/AlterEgoGod/ai-delegation.git',
            'source': relative, 'commit': commit, 'roles': role_hashes,
            'source_sha256': source_hashes, 'model_availability': 'not-verified'}
    plan[lock_path] = (json.dumps(lock, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    return plan, commit


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.ai-delegation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(source: Path, project: Path, check: bool = False) -> list[str]:
    plan, commit = build_plan(source, project)
    changed = {p: data for p, data in plan.items() if not p.exists() or p.read_bytes() != data}
    if check:
        if changed:
            raise ValueError('Installation missing or differs: ' + ', '.join(p.name for p in changed))
        print('Verified installed files and pinned commit: ' + commit)
        return []
    previous = {p: p.read_bytes() if p.exists() else None for p in changed}
    written = []
    try:
        for path, data in changed.items():
            write_atomic(path, data)
            written.append(path)
    except Exception:
        for path in reversed(written):
            if previous[path] is None:
                path.unlink()
            else:
                write_atomic(path, previous[path])
        raise
    print('Installed commit: ' + commit + '; changed files: ' + str(len(changed)))
    print('Model access is NOT verified. Start a new Codex session in a trusted project; verify active models.')
    return [str(p) for p in changed]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='Read-only verification; no model calls')
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parents[2], args.project.resolve(), args.check)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('Installation stopped: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
