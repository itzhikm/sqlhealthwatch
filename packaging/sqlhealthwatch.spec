# PyInstaller build spec -- a standalone collector for a host with no Python installed.
#
#   pip install pyinstaller
#   pyinstaller packaging/sqlhealthwatch.spec --clean --noconfirm
#
# Produces dist/sqlhealthwatch/ containing sqlhealthwatch.exe and its runtime.
#
# WHAT IS *NOT* BUNDLED, and cannot be:
#
#   Microsoft ODBC Driver 18 for SQL Server. pyodbc is only the Python binding; the driver itself is
#   a Windows system component installed by MSI. Every collector host still needs it, frozen exe or
#   not. Without it, every connection fails with "Data source name not found and no default driver".
#
# WHAT IS DELIBERATELY LEFT OUTSIDE THE EXE:
#
#   config/ and sql/. Both must stay editable -- onboarding a server means editing servers.yml, and
#   the whole point of keeping the DMV queries as files is that a DBA can tune them without a
#   rebuild. They are read relative to the --config directory, so the deployed layout is:
#
#       C:\sqlhealthwatch\
#           sqlhealthwatch.exe   (plus the _internal runtime folder)
#           config\             servers.yml, thresholds.yml, settings.yml, alerts.yml
#           sql\                the DMV queries
#           .env                secrets, if any server uses SQL auth
#
#   Run it with an absolute config path so the working directory cannot matter:
#       sqlhealthwatch.exe daily --config C:\sqlhealthwatch\config

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
PACKAGE = PROJECT_ROOT / "src" / "sqlhealthwatch"

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    # schema.sql lives inside the package and is applied by repository.bootstrap(), so unlike
    # config/ and sql/ it must travel inside the bundle.
    datas=[(str(PACKAGE / "storage" / "schema.sql"), "sqlhealthwatch/storage")],
    # pyodbc and pydantic-core are C extensions; pandas and pyarrow load submodules dynamically,
    # which static analysis does not always follow.
    hiddenimports=[
        "pyodbc",
        *collect_submodules("pydantic"),
        *collect_submodules("dotenv"),
    ],
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a window or a chart; excluding these keeps the bundle from ballooning.
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sqlhealthwatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # a scheduled task needs stdout/stderr and an exit code
    disable_windowed_traceback=False,
)

# --onedir, not --onefile: onefile unpacks the whole runtime to a temp directory on every launch,
# which for a job that runs every 15 minutes is pure overhead (and leaves temp litter if a run is
# killed). onedir starts immediately.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sqlhealthwatch",
)
