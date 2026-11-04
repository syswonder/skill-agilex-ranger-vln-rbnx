from glob import glob

from setuptools import find_packages, setup


PACKAGE_NAME = "internvln_navigation"


setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=("tests",)),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Origamii520",
    maintainer_email="Origamii520@users.noreply.github.com",
    description=(
        "RGB to InternVLA-N1 DualVLN HTTP bridge for the AgileX Ranger Mini V3, "
        "with a mutually exclusive navigation/VLN command-source mux and a "
        "Ranger chassis guard."
    ),
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "internvln_node = internvln_navigation.ros_node:main",
            "internvln_chassis_guard = internvln_navigation.chassis_guard_node:main",
        ],
    },
)
