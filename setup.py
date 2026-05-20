from pathlib import Path

from setuptools import setup, find_packages

BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS_FILE = (BASE_DIR / "requirements.txt").resolve()

if REQUIREMENTS_FILE.parent != BASE_DIR or not REQUIREMENTS_FILE.is_file():
	raise RuntimeError("Unable to safely resolve requirements.txt")

# nosemgrep: frappe-security-file-traversal - trusted local package metadata file.
install_requires = REQUIREMENTS_FILE.read_text(encoding="utf-8").strip().split("\n")

# get version from __version__ variable in taxjar_integration/__init__.py
from taxjar_integration import __version__ as version

setup(
	name="taxjar_integration",
	version=version,
	description="Taxjar Integration with ERPNext",
	author=" Frappe Technologies Pvt. Ltd.",
	author_email="hello@frappe.io",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires,
	python_requires=">=3.10"
)
