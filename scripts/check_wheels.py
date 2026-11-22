"""Check that every package in requirements.txt has a ready-made wheel on every system QC supports (macOS Apple Silicon
and Intel, Windows, Linux; Python 3.11-3.13), evaluating the platform markers the way each machine would.
Usage: python scripts/check_wheels.py requirements.txt   (exit 1 lists what would need compiling)"""
import subprocess, sys, tempfile
from packaging.requirements import Requirement
TARGETS = {  # name: (pip --platform tags, marker env)
    "macOS Apple Silicon": (["macosx_14_0_arm64"], {"sys_platform": "darwin", "platform_machine": "arm64", "platform_system": "Darwin"}),
    "macOS Intel": (["macosx_13_0_x86_64", "macosx_10_9_x86_64", "macosx_10_9_universal2", "macosx_10_12_x86_64"], {"sys_platform": "darwin", "platform_machine": "x86_64", "platform_system": "Darwin"}),
    "Windows": (["win_amd64"], {"sys_platform": "win32", "platform_machine": "AMD64", "platform_system": "Windows"}),
    "Linux": (["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux1_x86_64"], {"sys_platform": "linux", "platform_machine": "x86_64", "platform_system": "Linux"}),
}
lines = [l.split("#")[0].strip() for l in open(sys.argv[1]) if l.split("#")[0].strip()]
bad = 0
for name, (plats, env) in TARGETS.items():
    for py in ("3.11", "3.12", "3.13"):
        e = {**env, "python_version": py, "python_full_version": py + ".0", "implementation_name": "cpython", "os_name": "posix"}
        pins = [str(r).split(";")[0] for r in map(Requirement, lines) if r.marker is None or r.marker.evaluate(e)]
        with tempfile.TemporaryDirectory() as d:
            cmd = [sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:", "-d", d, "--python-version", py]
            for pl in plats:
                cmd += ["--platform", pl]
            missing = [p for p in pins if subprocess.run(cmd + [p], capture_output=True).returncode != 0]
        bad += len(missing)
        print(f"{name:20} py{py}: {'all ' + str(len(pins)) + ' packages have wheels' if not missing else 'MISSING ' + ', '.join(missing)}")
sys.exit(1 if bad else 0)
