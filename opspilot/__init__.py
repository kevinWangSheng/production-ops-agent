"""OpsPilot product package.

Read-only investigation product. Nothing here mutates an investigated system,
approves or blocks a release, or executes shell, SQL or cloud commands.
"""

# The product version. pyproject.toml ``version`` is its single source; this
# copy exists because the project is not installed as a package (no
# importlib.metadata), and scripts/release/check_version.py keeps them equal.
__version__ = "0.1.0"
