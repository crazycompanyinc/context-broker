from setuptools import setup, find_packages

setup(
    name="context-broker",
    version="1.0.0",
    description="Inter-Agent Communication Architecture — dependency graph and change propagation",
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "context-broker=context_broker:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Topic :: Software Development :: Libraries",
    ],
)
