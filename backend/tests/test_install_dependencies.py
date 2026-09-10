import importlib.util
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace


def test_dependency_check_rejects_missing_packages_versions_and_extras():
    path = Path(__file__).resolve().parents[2] / 'scripts/check-python-deps.py'
    spec = importlib.util.spec_from_file_location('install_dependencies', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    packages = {'server': SimpleNamespace(version='2.0', requires=['worker>=1; extra == "standard"'])}

    def lookup(name):
        if name not in packages:
            raise metadata.PackageNotFoundError(name)
        return packages[name]

    assert module.satisfied(['server>=1'], lookup)
    assert not module.satisfied(['missing>=1'], lookup)
    assert not module.satisfied(['server>=3'], lookup)
    assert not module.satisfied(['server[standard]>=1'], lookup)
    packages['worker'] = SimpleNamespace(version='1.0', requires=[])
    assert module.satisfied(['server[standard]>=1'], lookup)
