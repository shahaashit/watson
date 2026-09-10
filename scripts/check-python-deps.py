"""Check installed requirement versions and extras without network access."""
import sys
from importlib import metadata
from pip._vendor.packaging.requirements import Requirement


def satisfied(lines, distribution=metadata.distribution):
    pending = [Requirement(line.strip()) for line in lines
               if line.strip() and not line.lstrip().startswith('#')]
    seen = set()
    while pending:
        requirement = pending.pop()
        key = str(requirement)
        if key in seen:
            continue
        seen.add(key)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = distribution(requirement.name)
        except metadata.PackageNotFoundError:
            return False
        if not requirement.specifier.contains(installed.version, prereleases=True):
            return False
        for raw in installed.requires or []:
            dependency = Requirement(raw)
            if dependency.marker and not any(
                dependency.marker.evaluate({'extra': extra})
                for extra in ({''} | requirement.extras)
            ):
                continue
            dependency.marker = None  # Already evaluated in its parent extras context.
            pending.append(dependency)
    return True


if __name__ == '__main__':
    try:
        with open(sys.argv[1]) as source:
            valid = satisfied(source)
    except Exception:
        valid = False  # A malformed/unsupported requirement must reinstall, not skip.
    raise SystemExit(0 if valid else 1)
