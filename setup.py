from setuptools import setup

setup(
    name="whatson",
    version="1.1.0",
    description="WhatsApp Web CLI tool powered by Playwright browser automation",
    author="David",
    py_modules=["whatson"],
    python_requires=">=3.9",
    install_requires=[
        "typer[all]>=0.9.0",
        "playwright>=1.40.0",
        "tinydb>=4.8.0",
        "pyyaml>=6.0",
        "qrcode>=7.0",
    ],
    entry_points={
        "console_scripts": [
            "whatson=whatson:app_entry",
            "wo=whatson:app_entry",
        ],
    },
)
