import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py as _build_py


class BuildWithCMake(_build_py):
    def run(self):
        # When building the docs we don't want to build all other libraries
        if os.environ.get("BUILDING_DOCS") == "1":
            print("Skipping build of C/C++ libraries: only building the docs.")
            super().run()
            return

        project_root = Path(__file__).resolve().parent
        sim2lab_path = project_root / "src" / "spinnaker2" / "libs"
        experiment_runner_project_dir = sim2lab_path / "host" / "experiment" / "app"

        # Resolve full paths to executables
        cmake = shutil.which("cmake")
        make = shutil.which("make")
        python = shutil.which("python")

        if cmake is None or make is None:
            print("ERROR: 'cmake' and/or 'make' not found in PATH.")
            sys.exit(1)

        # Run cmake and make in experiment_runner_project_dir
        # Bandit S603: safe usage — no untrusted input or shell=True
        subprocess.run([cmake, "."], cwd=experiment_runner_project_dir, check=True)  # noqa: S603
        subprocess.run([make], cwd=experiment_runner_project_dir, check=True)  # noqa: S603

        # Build each app
        subprocess.run([python, experiment_runner_project_dir.parent / "make_all_apps.py"], check=True)  # noqa: S603

        super().run()


setup(
    name="spinnaker2",
    version="0.1",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    cmdclass={"build_py": BuildWithCMake},
    include_package_data=True,  # include files as specified in MANIFEST.in
)
