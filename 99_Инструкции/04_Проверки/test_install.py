"""Integration tests: temporary Git repos, no network and no model calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer', SOURCE / '99_Инструкции/03_Установка/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ai-delegation-test-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'Проект с пробелами'
        self.project.mkdir()
        self.git(self.project, 'init', '-q')
        self.source = self.project / '.module'
        self.source.mkdir()
        shutil.copytree(SOURCE / '99_Инструкции', self.source / '99_Инструкции',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.git(self.source, 'init', '-q')
        self.commit_source()

    def git(self, root, *args):
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Installer Test',
                        '-c', 'user.email=test@example.invalid', *args],
                       check=True, capture_output=True)

    def commit_source(self):
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-qm', 'fixture')

    def put(self, relative, text):
        target = self.project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()
                and '.git' not in p.parts and '.module' not in p.parts}

    def test_install_preserves_and_is_idempotent(self):
        self.put('AGENTS.md', '# Existing instructions\nKeep these.\n')
        self.put('.codex/config.toml', '# Preserve comment\n[windows]\nsandbox = "elevated"\n')
        installer.install(self.source, self.project)
        before = self.snapshot()
        self.assertEqual(installer.install(self.source, self.project), [])
        installer.install(self.source, self.project, check=True)
        self.assertEqual(before, self.snapshot())
        self.assertIn('Keep these.', (self.project / 'AGENTS.md').read_text(encoding='utf-8'))
        config = (self.project / '.codex/config.toml').read_text()
        self.assertIn('# Preserve comment', config)
        self.assertEqual(tomllib.loads(config)['windows']['sandbox'], 'elevated')
        self.assertEqual(tomllib.loads(config)['model'], 'gpt-6-astra')

    def test_conflict_has_no_partial_writes(self):
        self.put('.codex/config.toml', 'model = "other"\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting config'):
            installer.install(self.source, self.project)
        self.assertEqual(before, self.snapshot())

    def test_existing_agents_table_preserved(self):
        self.put('.codex/config.toml', '[agents] # mine\ninterrupt_message = false\n[other]\nvalue = 7\n')
        installer.install(self.source, self.project)
        config = tomllib.loads((self.project / '.codex/config.toml').read_text())
        self.assertFalse(config['agents']['interrupt_message'])
        self.assertEqual(config['other']['value'], 7)

    def test_modified_role_not_overwritten(self):
        installer.install(self.source, self.project)
        path = '.codex/agents/delegation-luna-worker.toml'
        self.put(path, '# my changes\n' + (self.project / path).read_text())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Role has existing/user changes'):
            installer.install(self.source, self.project)
        self.assertEqual(before, self.snapshot())

    def test_missing_install_check_is_read_only(self):
        with self.assertRaisesRegex(ValueError, 'Installation missing'):
            installer.install(self.source, self.project, check=True)
        self.assertFalse((self.project / '.codex').exists())

    def test_dirty_source_rejected(self):
        (self.source / 'new.txt').write_text('uncommitted')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            installer.install(self.source, self.project)

    def test_committed_upgrade_updates_owned_role_and_lock(self):
        installer.install(self.source, self.project)
        role = self.source / '99_Инструкции/02_Конфигурация/delegation-luna-worker.toml'
        role.write_text(role.read_text() + '\n# New release\n')
        self.commit_source()
        with self.assertRaisesRegex(ValueError, 'Installation missing or differs'):
            installer.install(self.source, self.project, check=True)
        installer.install(self.source, self.project)
        installer.install(self.source, self.project, check=True)
        self.assertIn('New release', (self.project / '.codex/agents' / role.name).read_text())

    def test_override_rejected_before_writes(self):
        self.put('AGENTS.override.md', '# Override')
        with self.assertRaisesRegex(ValueError, 'AGENTS.override.md'):
            installer.install(self.source, self.project)
        self.assertFalse((self.project / '.codex').exists())

    def test_duplicate_role_name_rejected(self):
        self.put('.codex/agents/other.toml', 'name = "delegation-luna-worker"\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate custom agent name'):
            installer.install(self.source, self.project)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_write_failure_rolls_back_files(self):
        self.put('.codex/config.toml', '# Keep\n')
        before = self.snapshot()
        actual = installer.write_atomic
        calls = 0

        def failing(path, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('simulated write failure')
            return actual(path, data)

        with patch.object(installer, 'write_atomic', side_effect=failing):
            with self.assertRaisesRegex(OSError, 'simulated'):
                installer.install(self.source, self.project)
        self.assertEqual(before, self.snapshot())

    def test_external_source_rejected(self):
        with self.assertRaisesRegex(ValueError, 'inside the target project'):
            installer.install(self.project.parent, self.project)

    def test_wrong_toml_type_rejected(self):
        self.put('.codex/config.toml', '[agents]\nenabled = 1\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting config'):
            installer.install(self.source, self.project)

    def test_malformed_managed_block_rejected(self):
        self.put('AGENTS.md', installer.BEGIN)
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            installer.install(self.source, self.project)
        self.assertFalse((self.project / '.codex').exists())


if __name__ == '__main__':
    unittest.main()
