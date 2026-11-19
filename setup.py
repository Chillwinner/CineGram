from __future__ import annotations

from setuptools import find_packages, setup

setup(
    name="cinevec-telegram-plugin",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={"tg_plugin": ["static/*"]},
    include_package_data=True,
    entry_points={"console_scripts": ["tg-plugin=tg_plugin.main:cli"]},
)
