from setuptools import setup

setup(
    name="dbt-spark-qualify",
    version="0.1.0",
    description="Invisible dbt-spark patch to enable QUALIFY support via sqlglot",
    py_modules=["dbt_qualify_patch"],
    # This places the .pth file directly into site-packages
    data_files=[("", ["dbt_spark_qualify.pth"])],
    install_requires=[
        "sqlglot",
        "dbt-spark"
    ]
)