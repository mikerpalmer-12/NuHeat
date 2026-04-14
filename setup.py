from setuptools import setup, find_packages

setup(
    name="nuheat-control",
    version="0.1.0",
    description="Control NuHeat floor heating thermostats via CLI and REST API",
    packages=find_packages(),
    package_data={"nuheat": ["static/*.html"]},
    python_requires=">=3.11",
    install_requires=[
        "aiohttp>=3.9,<4",
        "fastapi>=0.110,<1",
        "pydantic>=2.0,<3",
        "uvicorn>=0.29,<1",
    ],
    entry_points={
        "console_scripts": [
            "nuheat=nuheat.cli:main",
        ],
    },
)
