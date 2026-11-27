"""Report outdated or vulnerable dependencies. Read-only; needs the network.

    .venv/bin/python tools/check_deps.py

Checks, from the repo root's point of view:
- the pins in requirements*.txt against the latest release on PyPI;
- every package that ships in the release app (.build/release, from
  `zsh build.sh --release`), pins and their dependencies, against known vulnerabilities
  in OSV (https://osv.dev), and the same for this interpreter's packages (the .venv),
  which are development tools only;
- the python-build-standalone release pinned in build.sh against the newest one;
- the GitHub Actions in .github against their latest major version.

Exits 1 if anything in the release app is vulnerable, 0 otherwise. Outdated versions
are reported for a person to judge: a newer one may be held back on purpose.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
RELEASE_PYTHON = ROOT / ".build/release/Audio Sorter.app/Contents/Resources/python/bin/python3"
# Name, version and requirements of every installed distribution, as JSON.
LIST = ("import importlib.metadata as m, json; print(json.dumps(sorted("
        "(d.metadata['Name'].lower(), d.version, d.requires or []) for d in m.distributions())))")


def get(url, data=None):
    request = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                     headers={"Content-Type": "application/json", "User-Agent": "audio-sorter-check-deps"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pins():
    for name in ("requirements.txt", "requirements-detect.txt"):
        for line in (ROOT / name).read_text().splitlines():
            match = re.match(r"([A-Za-z0-9_.-]+)==(\S+)", line.strip())
            if match:
                yield name, match[1], match[2]


def audit(label, python, environment=None):
    """Prints known vulnerabilities and outdated unpinned packages; returns the vulnerable count."""
    installed = json.loads(subprocess.run([str(python), "-c", LIST], env=environment,
                                          capture_output=True, text=True, check=True).stdout)
    results = get("https://api.osv.dev/v1/querybatch", {"queries": [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version} for name, version, _ in installed]})["results"]
    vulnerable = [(name, version, [v["id"] for v in result.get("vulns", [])])
                  for (name, version, _), result in zip(installed, results) if result.get("vulns")]
    print(f"\n{label}: {len(installed)} packages, {len(vulnerable)} with known vulnerabilities (OSV)")
    for name, version, ids in vulnerable:
        print(f"  {name} {version}: {', '.join(ids)}")
    # Show what holds an unpinned package back, so a newer version on PyPI isn't mistaken
    # for a missed update.
    pinned = {name.lower() for _, name, _ in pins()}
    for name, version, _ in installed:
        if name in pinned:
            continue
        latest = get(f"https://pypi.org/pypi/{name}/json")["info"]["version"]
        if latest != version:
            held = [f"{parent} needs {req.split(';')[0]}" for parent, _, requires in installed
                    for req in requires if re.match(rf"{re.escape(name)}\b", req, re.I) and "extra ==" not in req]
            print(f"  unpinned {name} {version}, {latest} available" + (f" ({'; '.join(held)})" if held else ""))
    return len(vulnerable)


def main():
    print("Pinned packages (PyPI):")
    for file, name, version in pins():
        latest = get(f"https://pypi.org/pypi/{name}/json")["info"]["version"]
        note = "up to date" if latest == version else f"{latest} available"
        print(f"  {name:24} {version:12} {note}  ({file})")

    if RELEASE_PYTHON.exists():
        # The app runs its Python with an otherwise empty environment, as here.
        shipped = audit("Release app (ships to testers)", RELEASE_PYTHON, {"PYTHONNOUSERSITE": "1"})
    else:
        shipped = 0
        print("\nRelease app: not built; run zsh build.sh --release to check what ships.")
    audit("This interpreter (development only)", sys.executable, os.environ)

    build = (ROOT / "build.sh").read_text()
    pinned_python = re.search(r"cpython-(3\.\d+)\.(\d+)\+(\d+)-aarch64-apple-darwin-install_only_stripped", build)
    release = get("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")
    newest = sorted((int(m[1]), m[0]) for m in (re.match(rf"cpython-{re.escape(pinned_python[1])}\.(\d+)\+\d+-aarch64-apple-darwin-install_only_stripped\.tar\.gz$", a["name"])
                                                for a in release["assets"]) if m)
    print("\nStandalone Python (build.sh):")
    current = f"{pinned_python[1]}.{pinned_python[2]}+{pinned_python[3]}"
    latest = f"{pinned_python[1]}.{newest[-1][0]}+{release['tag_name']}" if newest else "unknown"
    print(f"  {current}  " + ("up to date" if latest == current else f"{latest} available"))

    print("\nGitHub Actions:")
    used = sorted({m for f in (ROOT / ".github").rglob("*.yml") for m in re.findall(r"uses: ([\w-]+/[\w-]+)@(v\d+)", f.read_text())})
    for action, major in used:
        tag = get(f"https://api.github.com/repos/{action}/releases/latest")["tag_name"]
        note = "up to date" if tag.split(".")[0] == major else f"{tag} available"
        print(f"  {action:32} {major:4} {note}")

    return 1 if shipped else 0


if __name__ == "__main__":
    sys.exit(main())
