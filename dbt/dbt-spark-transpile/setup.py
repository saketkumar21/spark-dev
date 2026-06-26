"""Shim setup.py — metadata lives in pyproject.toml.

This file exists for ONE reason: to place ``dbt_spark_transpile.pth`` into the wheel's
purelib (site-packages) so the patch auto-activates on interpreter start-up.

Why this is needed: a ``.pth`` is the standard way to run code at start-up, but it only
fires when it lands in a directory on ``sys.path`` (site-packages). The obvious
``data_files`` route puts it in the venv's *data* scheme (the prefix root), which is NOT
on ``sys.path`` under uv/modern pip, so it never loads. Copying it next to the top-level
module in the build lib makes the wheel install it into site-packages, where it loads.

Everything else (name, version, deps, py-modules, console script) is declared in
pyproject.toml; setuptools merges this ``cmdclass`` with that metadata.
"""
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

PTH = "dbt_spark_transpile.pth"


class build_py_with_pth(build_py):
    def run(self):
        super().run()
        shutil.copyfile(PTH, os.path.join(self.build_lib, PTH))


setup(cmdclass={"build_py": build_py_with_pth})
