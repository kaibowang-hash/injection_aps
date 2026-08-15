from pathlib import Path

from setuptools import find_packages, setup


def get_version() -> str:
	init_py = Path(__file__).parent / "injection_aps" / "__init__.py"
	for line in init_py.read_text().splitlines():
		if line.startswith("__version__"):
			return line.split("=")[1].strip().strip('"')
	return "0.0.2"


setup(
	name="injection_aps",
	version=get_version(),
	description="Injection APS for ERPNext manufacturing planning",
	author="JCE",
	author_email="kaibo_wang@whjichen.cn",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=["ortools==9.4.1874"],
)
