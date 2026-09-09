"""Install a checksum-pinned official CLI into an explicit task-local directory."""

import argparse
import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "8.30.1"
HASHES = {
    ("Darwin", "arm64"): (
        "darwin_arm64",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Linux", "x86_64"): (
        "linux_x64",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
}


def install(directory: Path) -> Path:
    asset, expected = HASHES[(platform.system(), platform.machine())]
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{asset}.tar.gz"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("CHECKSUM_MISMATCH")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "gitleaks"
    if directory.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError("DESTINATION_EXISTS")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile():
            raise ValueError("ARCHIVE_INVALID")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("ARCHIVE_INVALID")
        with target.open("xb") as output:
            output.write(source.read())
    target.chmod(0o700)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        install(args.directory)
    except Exception:
        print("GITLEAKS_INSTALL_FAILED")
        raise SystemExit(2) from None
    print("GITLEAKS_INSTALLED_8.30.1")
