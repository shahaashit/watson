"""Exercise the real installer with fake dependencies, never the live service."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('mirror', [False, True])
@pytest.mark.parametrize('pip_missing', [False, True])
def test_installer_handles_empty_and_populated_pip_args(tmp_path, mirror, pip_missing):
    root = tmp_path / 'checkout with spaces'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'install.sh'
    shutil.copyfile(source, scripts / 'install.sh')
    (root / 'frontend').mkdir()
    (root / 'frontend' / 'package.json').write_text('{}')
    (root / 'frontend' / 'package-lock.json').write_text('{}')
    bins = root / 'backend' / '.venv' / 'bin'
    bins.mkdir(parents=True)
    (root / 'backend' / 'requirements.txt').write_text('fastapi>=0.110\n')
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    log = tmp_path / 'pip-args'

    def executable(path, body):
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)

    # Python prerequisite/version checks succeed; pip calls only record argv.
    python = '''if [[ "$1" == "-" ]]; then /bin/cat >/dev/null; exit 0; fi
if [[ "$*" == "-m pip --version" ]]; then
  [[ "$WATSON_TEST_PIP_MISSING" != "1" || -f "$WATSON_TEST_PIP_LOG.bootstrapped" ]]; exit $?
fi
if [[ "$*" == "-m pip check" ]]; then exit 0; fi
if [[ "$1" == */check-python-deps.py ]]; then exit 0; fi
if [[ "$*" == "-m ensurepip" ]]; then touch "$WATSON_TEST_PIP_LOG.bootstrapped"; fi
if [[ "$1" == "-c" ]]; then exit 1; fi
printf '%s\\0' "$@" >> "$WATSON_TEST_PIP_LOG"
printf '\\n' >> "$WATSON_TEST_PIP_LOG"
'''
    executable(fake_bin / 'python3', python)
    executable(bins / 'python', python)
    executable(fake_bin / 'uname', 'echo Darwin\n')
    executable(fake_bin / 'node', 'echo 22\n')
    executable(fake_bin / 'npm', '''echo "$*" >> "$WATSON_TEST_NPM_LOG"
if [[ "$1" == "ci" ]]; then mkdir -p node_modules; fi
if [[ "$*" == "run build" ]]; then mkdir -p dist; touch dist/index.html dist/app.js; fi
exit 0
''')
    executable(scripts / 'install-launch-agent.sh', 'exit 0\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PIP_', 'WATSON_', 'NPM_CONFIG_'))}
    npm_log = tmp_path / 'npm-args'
    env.update(PATH=f'{fake_bin}:/usr/bin:/bin', WATSON_TEST_PIP_LOG=str(log), WATSON_TEST_NPM_LOG=str(npm_log), WATSON_TEST_PIP_MISSING='1' if pip_missing else '0')
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
    assert calls == ([['-m', 'ensurepip']] if pip_missing else []) + [
        ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(root / 'backend' / 'requirements.txt'), *extra],
    ]
    first_pip = log.read_text()
    second = subprocess.run(['/bin/bash', str(scripts / 'install.sh')],
                            env=env, capture_output=True, text=True, timeout=15)
    assert second.returncode == 0, second.stderr
    assert log.read_text() == first_pip
    executable(bins / 'python', 'exit 1\n')
    invalid = subprocess.run(['/bin/bash', str(scripts / 'install.sh')],
                             env=env, capture_output=True, text=True, timeout=15)
    assert invalid.returncode != 0
    assert 'Existing backend/.venv is incompatible' in invalid.stderr
    assert log.read_text() == first_pip
    executable(bins / 'python', python)
    assert npm_log.read_text().splitlines().count('ci --prefer-offline --no-audit --no-fund') == 1
    assert npm_log.read_text().splitlines().count('run build') == 1
    assert 'Frontend unchanged; reusing build' in second.stdout
    (root / 'frontend' / 'index.html').write_text('changed source')
    third = subprocess.run(['/bin/bash', str(scripts / 'install.sh')],
                           env=env, capture_output=True, text=True, timeout=15)
    assert third.returncode == 0, third.stderr
    assert npm_log.read_text().splitlines().count('run build') == 2
    assert log.read_text() == first_pip
    (root / 'frontend' / 'dist' / 'app.js').unlink()
    repaired = subprocess.run(['/bin/bash', str(scripts / 'install.sh')],
                              env=env, capture_output=True, text=True, timeout=15)
    assert repaired.returncode == 0, repaired.stderr
    assert (root / 'frontend' / 'dist' / 'app.js').exists()
    assert npm_log.read_text().splitlines().count('run build') == 3
