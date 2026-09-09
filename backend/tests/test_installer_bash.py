"""Exercise the real installer with fake dependencies, never the live service."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('mirror', [False, True])
def test_installer_handles_empty_and_populated_pip_args(tmp_path, mirror):
    root = tmp_path / 'checkout with spaces'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'install.sh'
    shutil.copyfile(source, scripts / 'install.sh')
    (root / 'frontend').mkdir()
    bins = root / 'backend' / '.venv' / 'bin'
    bins.mkdir(parents=True)
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    log = tmp_path / 'pip-args'

    def executable(path, body):
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)

    # Python prerequisite/version checks succeed; pip calls only record argv.
    python = '''if [[ "$1" == "-" ]]; then /bin/cat >/dev/null; exit 0; fi
if [[ "$1" == "-c" ]]; then exit 1; fi
printf '%s\\0' "$@" >> "$WATSON_TEST_PIP_LOG"
printf '\\n' >> "$WATSON_TEST_PIP_LOG"
'''
    executable(fake_bin / 'python3', python)
    executable(bins / 'python', python)
    executable(fake_bin / 'uname', 'echo Darwin\n')
    executable(fake_bin / 'node', 'echo 22\n')
    executable(fake_bin / 'npm', 'exit 0\n')
    executable(scripts / 'install-launch-agent.sh', 'exit 0\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PIP_', 'WATSON_', 'NPM_CONFIG_'))}
    env.update(PATH=f'{fake_bin}:/usr/bin:/bin', WATSON_TEST_PIP_LOG=str(log))
    if mirror:
        (root / '.watson-install.env').write_text(
            'PIP_INDEX_URL=https://packages.example.com/simple\n'
            'PIP_TRUSTED_HOST=packages.example.com\n')
    result = subprocess.run(['/bin/bash', str(scripts / 'install.sh')],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    calls = [line.removesuffix('\0').split('\0') for line in log.read_text().splitlines()]
    extra = ['--index-url', 'https://packages.example.com/simple',
             '--trusted-host', 'packages.example.com'] if mirror else []
    assert calls == [
        ['-m', 'pip', 'install', '--upgrade', 'pip', *extra],
        ['-m', 'pip', 'install', '-r', str(root / 'backend' / 'requirements.txt'), *extra],
    ]
