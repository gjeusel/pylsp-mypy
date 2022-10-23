# -*- coding: utf-8 -*-
"""
File that contains the python-lsp-server plugin pylsp-mypy.

Created on Fri Jul 10 09:53:57 2020

@author: Richard Kellnberger
"""
import ast
import atexit
import collections
import logging
import os
import os.path
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import toml
from mypy import api as mypy_api
from pylsp import hookimpl
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

line_pattern: str = r"((?:^[a-z]:)?[^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)"

log = logging.getLogger(__name__)

# A mapping from workspace path to config file path
mypy_config_file_map: Dict[str, Optional[str]] = {}

# In non-live-mode the file contents aren't updated.
# Returning an empty diagnostic clears the diagnostic result,
# so store a cache of last diagnostics for each file a-la the pylint plugin,
# so we can return some potentially-stale diagnostics.
# https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
last_diagnostics: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)

# Windows started opening opening a cmd-like window for every subprocess call
# This flag prevents that.
# This flag is new in python 3.7
# THis flag only exists on Windows
windows_flag: Dict[str, int] = (
    # type: ignore
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if os.name == "nt"
    else {}
)

DMYPY_TMP_DIR = Path(tempfile.gettempdir())
dmypy_status_file = DMYPY_TMP_DIR / "dmypy.json"
dmypy_perf_file = DMYPY_TMP_DIR / "dmypy-perf.json"


_runtime_filepaths: dict[str, Any] = {
    # "live_mode_buffer": None,
    # "dmypy_status_file": None,
    # "dmypy_perf_file": None,
}


def get_runtime_filepaths() -> dict[str, Any]:
    global _runtime_filepaths
    tmpdir = Path(tempfile.gettempdir())
    if not _runtime_filepaths:
        _runtime_filepaths = {
            "live_mode_buffer": tmpdir / "live-mode-buffer.txt",
            "dmypy_status_file": tmpdir / "dmypy.json",
            "dmypy_perf_file": tmpdir / "dmypy-perf.json",
        }

    return _runtime_filepaths


def format_diagnostics_log_report(diagnostics: list[Any]) -> str:
    map_diag_severity = collections.defaultdict(list)
    for diag in diagnostics:
        map_diag_severity[str(diag["severity"])].append(diag)

    log_msg_details = []
    for key, label in {"1": "error", "2": "warning", "3": "info"}.items():
        if key in map_diag_severity:
            log_msg_details.append(f"{label}: {len(map_diag_severity[key])}")

    msg = f"total: {len(diagnostics)}"
    if log_msg_details:
        msg = f'{msg} - ({" ".join(log_msg_details)})'

    return msg


def parse_line(line: str, document: Optional[Document] = None) -> Optional[Dict[str, Any]]:
    """
    Return a language-server diagnostic from a line of the Mypy error report.

    optionally, use the whole document to provide more context on it.


    Parameters
    ----------
    line : str
        Line of mypy output to be analysed.
    document : Optional[Document], optional
        Document in wich the line is found. The default is None.

    Returns
    -------
    Optional[Dict[str, Any]]
        The dict with the lint data.

    """
    result = re.match(line_pattern, line)
    if result:
        file_path, linenoStr, offsetStr, severity, msg = result.groups()

        if file_path != "<string>":  # live mode
            # results from other files can be included, but we cannot return them.
            if document and document.path and not document.path.endswith(file_path):
                log.debug(
                    f"diagnostics - discarding result for {file_path} against {document.path}"
                )
                return None

        lineno = int(linenoStr or 1) - 1  # 0-based line number
        offset = int(offsetStr or 1) - 1  # 0-based offset
        errno = 2
        if severity == "error":
            errno = 1
        diag: Dict[str, Any] = {
            "source": "mypy",
            "range": {
                "start": {"line": lineno, "character": offset},
                # There may be a better solution, but mypy does not provide end
                "end": {"line": lineno, "character": offset + 1},
            },
            "message": msg,
            "severity": errno,
        }
        if document:
            # although mypy does not provide the end of the affected range, we
            # can make a good guess by highlighting the word that Mypy flagged
            word = document.word_at_position(diag["range"]["start"])
            if word:
                diag["range"]["end"]["character"] = diag["range"]["start"]["character"] + len(word)

        return diag
    return None


def apply_overrides(args: List[str], overrides: List[Any]) -> List[str]:
    """Replace or combine default command-line options with overrides."""
    overrides_iterator = iter(overrides)
    if True not in overrides_iterator:
        return overrides
    # If True is in the list, the if above leaves the iterator at the element after True,
    # therefore, the list below only contains the elements after the True
    rest = list(overrides_iterator)
    # slice of the True and the rest, add the args, add the rest
    return overrides[: -(len(rest) + 1)] + args + rest


@hookimpl
def pylsp_lint(
    config: Config, workspace: Workspace, document: Document, is_saved: bool
) -> List[Dict[str, Any]]:
    """
    Lints.

    Parameters
    ----------
    config : Config
        The pylsp config.
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    settings = config.plugin_settings("pylsp_mypy")
    oldSettings1 = config.plugin_settings("mypy-ls")
    if oldSettings1 != {}:
        raise DeprecationWarning(
            "Your configuration uses the namespace mypy-ls, this should be changed to pylsp_mypy"
        )
    oldSettings2 = config.plugin_settings("mypy_ls")
    if oldSettings2 != {}:
        raise DeprecationWarning(
            "Your configuration uses the namespace mypy_ls, this should be changed to pylsp_mypy"
        )
    if settings == {}:
        settings = oldSettings1
        if settings == {}:
            settings = oldSettings2

    log.info(f"lint cfg - {settings=} {document.path=} {is_saved=}")

    live_mode = settings.get("live_mode", True)
    dmypy = settings.get("dmypy", False)

    if dmypy and live_mode:
        # dmypy can only be efficiently run on files that have been saved, see:
        # https://github.com/python/mypy/issues/9309
        log.warning("cfg - live_mode is not supported with dmypy, disabling")
        live_mode = False

    args = ["--show-column-numbers"]

    runtime_filepaths = get_runtime_filepaths()

    if live_mode and not is_saved:
        live_mode_buffer_file = open(runtime_filepaths["live_mode_buffer"], "w")
        log.info(f"cfg - live_mode {live_mode_buffer_file=}")
        live_mode_buffer_file.write(document.source)
        live_mode_buffer_file.close()
        args.extend(["--shadow-file", document.path, live_mode_buffer_file.name])
    elif not is_saved and document.path in last_diagnostics:
        # On-launch the document isn't marked as saved, so fall through and run
        # the diagnostics anyway even if the file contents may be out of date.
        n_diagnostics = len(last_diagnostics[document.path])
        log.info(f"cache - returning cached diagnostics {n_diagnostics=}")
        return last_diagnostics[document.path]

    mypy_cfg_file = mypy_config_file_map.get(workspace.root_path)
    if mypy_cfg_file:
        args.append("--config-file")
        args.append(mypy_cfg_file)

    args.append(document.path)

    if settings.get("strict", False):
        args.append("--strict")

    overrides = settings.get("overrides", [True])
    status = 0

    if not dmypy:
        args.extend(["--incremental", "--follow-imports", "silent"])
        args = apply_overrides(args, overrides)
        log.info(f"mypy - {args=}")
        report, messages, status = mypy_api.run(args)
    else:
        # If dmypy daemon is non-responsive calls to run will block.
        # Check daemon status, if non-zero daemon is dead or hung.
        # If daemon is hung, kill will reset
        # If daemon is dead/absent, kill will no-op.
        # In either case, reset to fresh state

        _, messages, status = mypy_api.run_dmypy(["status"])
        if status != 0:
            log.info(f"dmypy status - {status=} ({messages=})")
            mypy_api.run_dmypy(["kill"])

        # run to use existing daemon or restart if required
        dmypy_args = ["--status-file", runtime_filepaths["dmypy_status_file"].as_posix()]
        dmypy_subcommand_args = [
            "--perf-stats-file",
            runtime_filepaths["dmypy_perf_file"].as_posix(),
        ]

        args = [*dmypy_args, "run", *dmypy_subcommand_args, "--"] + apply_overrides(args, overrides)

        log.info(f"dmypy - running 'dmypy {' '.join(args)}'")
        report, messages, status = mypy_api.run_dmypy(args)

    log.debug(f"report:\n{report}")
    log.debug(f"errors:\n{messages}")

    diagnostics = []

    # Expose generic mypy error on the first line.
    if messages:
        diagnostics.append(
            {
                "source": "mypy",
                "range": {
                    "start": {"line": 0, "character": 0},
                    # Client is supposed to clip end column to line length.
                    "end": {"line": 0, "character": 1000},
                },
                "message": messages,
                # Error if exited with error or warning.
                "severity": 1 if status != 0 else 2,
            }
        )

    for line in report.splitlines():
        log.debug(f"parsing - {line=}")
        diag = parse_line(line, document)
        if diag:
            diagnostics.append(diag)

    log.info(f"diagnostics - {format_diagnostics_log_report(diagnostics)}")

    last_diagnostics[document.path] = diagnostics
    return diagnostics


@hookimpl
def pylsp_settings(config: Config) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Read the settings.

    Parameters
    ----------
    config : Config
        The pylsp config.

    Returns
    -------
    Dict[str, Dict[str, Dict[str, str]]]
        The config dict.

    """
    configuration = init(config._root_path)
    return {"plugins": {"pylsp_mypy": configuration}}


def init(workspace: str) -> Dict[str, str]:
    """
    Find plugin and mypy config files and creates the temp file should it be used.

    Parameters
    ----------
    workspace : str
        The path to the current workspace.

    Returns
    -------
    Dict[str, str]
        The plugin config dict.

    """
    log.info(f"init workspace - {workspace}")

    configuration = {}
    path = find_config_file(
        workspace, ["pylsp-mypy.cfg", "mypy-ls.cfg", "mypy_ls.cfg", "pyproject.toml"]
    )
    if path:
        if "pyproject.toml" in path:
            configuration = toml.load(path).get("tool").get("pylsp-mypy")
        else:
            with open(path) as file:
                configuration = ast.literal_eval(file.read())

    mypy_config_file = find_config_file(workspace, ["mypy.ini", ".mypy.ini", "pyproject.toml"])
    mypy_config_file_map[workspace] = mypy_config_file

    log.info(f"mypy cfg - {mypy_config_file=} {configuration=}")
    return configuration


def find_config_file(path: str, names: List[str]) -> Optional[str]:
    """
    Search for a config file.

    Search for a file of a given name from the directory specifyed by path through all parent
    directories. The first file found is selected.

    Parameters
    ----------
    path : str
        The path where the search starts.
    names : List[str]
        The file to be found (or alternative names).

    Returns
    -------
    Optional[str]
        The path where the file has been found or None if no matching file has been found.

    """
    start = Path(path).joinpath(names[0])  # the join causes the parents to include path
    for parent in start.parents:
        for name in names:
            file = parent.joinpath(name)
            if file.is_file():
                if file.name in ["mypy-ls.cfg", "mypy_ls.cfg"]:
                    raise DeprecationWarning(
                        f"{str(file)}: {file.name} is no longer supported, you should rename your "
                        "config file to pylsp-mypy.cfg or preferably use a pyproject.toml instead."
                    )
                if file.name == "pyproject.toml":
                    isPluginConfig = "pylsp-mypy.cfg" in names
                    configPresent = (
                        toml.load(file)
                        .get("tool", {})
                        .get("pylsp-mypy" if isPluginConfig else "mypy")
                        is not None
                    )
                    if not configPresent:
                        continue
                return str(file)

    return None


@atexit.register
def close() -> None:
    """
    Deltes the tempFile should it exist.

    Returns
    -------
    None.

    """
    for runtime_fpath in _runtime_filepaths.values():
        if runtime_fpath:
            os.unlink(runtime_fpath)
