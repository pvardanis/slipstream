"""Read a sweep or cell config YAML, bind the CLI execution context, and validate it.

The config models (``CellConfig``, ``SweepConfig``, ``RESERVED_KEYS``) are the contract
kernel; these loaders are the executor's boundary that reads the file, injects the
CLI-supplied execution context (endpoint, served model, output dir, commercial flag — the
served model's single source of truth is model.yaml), guards the CLI-injected keys the
config must never set, and rejects a malformed config before the run touches the endpoint.
"""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError
from slipstream_bench.contract.config import (
    RESERVED_KEYS,
    CellConfig,
    Knobs,
    SweepConfig,
    SweepError,
)

# The config model a loader validates against: a whole-grid SweepConfig or a single
# CellConfig, both Knobs subclasses. Binds _load_config's return to the model passed in.
_KnobsT = TypeVar("_KnobsT", bound=Knobs)


def _read_config_mapping(path: Path, *, label: str) -> dict[str, object]:
    """Read a config YAML at ``path`` into a mapping, guarding the reserved keys.

    The shared boundary both loaders run: read the file, parse it, name an empty or
    non-mapping document, and reject a config that sets any CLI-injected key. The
    caller layers on the execution context and validates against its own model.

    :param path: the config YAML file.
    :param label: the config kind named in the error messages (e.g. "sweep config",
        "cell config"), so a failure names the artifact the caller actually passed.
    :return: the parsed mapping, with no reserved key set.
    :raise SweepError: on a read/parse failure, an empty or non-mapping document, or
        a reserved key set.
    """
    try:
        text = path.read_text()
    except FileNotFoundError as error:
        raise SweepError(f"{label} not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SweepError(f"{label} could not be read: {path}: {error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SweepError(f"{path} is not valid YAML: {error}") from error
    # safe_load returns None for an empty or comment-only file without raising; name
    # that here so the run aborts on a clear message, not an opaque "input should be
    # a mapping" from validating None.
    if data is None:
        raise SweepError(f"{label} is empty: {path}")
    if not isinstance(data, dict):
        raise SweepError(f"{label} must be a mapping: {path}")
    reserved = [key for key in RESERVED_KEYS if key in data]
    if reserved:
        raise SweepError(
            f"{path} sets CLI-injected key(s) {', '.join(reserved)}: these come from "
            "the command line (model.yaml is the served-model source of truth), not "
            "the config file"
        )
    return data


def _load_config(
    path: Path,
    model_cls: type[_KnobsT],
    *,
    label: str,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    tokenizer: str | None = None,
) -> _KnobsT:
    """Read a config YAML at ``path``, bind the execution context, and validate it.

    The shared body both loaders run: the endpoint, served model, output dir, and
    commercial flag are injected from the CLI, since the served model's single source
    of truth is model.yaml. A config that sets any reserved key, is missing,
    unreadable, empty, not a YAML mapping, or fails validation is rejected here,
    before the run touches the endpoint.

    :param path: the config YAML file.
    :param model_cls: the model the mapping validates against (SweepConfig/CellConfig).
    :param label: the config kind named in error messages (e.g. "sweep config").
    :param base_url: the endpoint the run targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for the per-cell result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param tokenizer: a prompt-synthesis tokenizer to bind, overriding any the YAML
        authors; the self-hosted arm injects the image's baked snapshot path here so an
        offline ``vllm bench serve`` resolves it. ``None`` leaves the YAML value intact.
    :return: the validated config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    data = _read_config_mapping(path, label=label)
    injected_tokenizer = {"tokenizer": tokenizer} if tokenizer is not None else {}
    try:
        return model_cls.model_validate(
            {
                **data,
                **injected_tokenizer,
                "base_url": base_url,
                "model": model,
                "out_dir": out_dir,
                "commercial": commercial,
            }
        )
    except ValidationError as error:
        raise SweepError(f"invalid {label} ({path}):\n{error}") from error


def load_sweep_config(
    path: Path,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    tokenizer: str | None = None,
) -> SweepConfig:
    """Read the sweep definition at ``path`` (grid axes + shared knobs) and validate it.

    The YAML carries only what defines the experiment (the grid axes, lengths, SLO,
    seed); the execution context is injected from the CLI. See :func:`_load_config`.

    :param path: the config YAML file, e.g. bench/load-sweep.yaml.
    :param base_url: the endpoint the sweep targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for the per-cell result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param tokenizer: a prompt-synthesis tokenizer to bind (the baked snapshot path on
        the self-hosted arm); None leaves the YAML value intact.
    :return: the validated config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    return _load_config(
        path,
        SweepConfig,
        label="sweep config",
        base_url=base_url,
        model=model,
        out_dir=out_dir,
        commercial=commercial,
        tokenizer=tokenizer,
    )


def load_cell_config(
    path: Path,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    tokenizer: str | None = None,
) -> CellConfig:
    """Read the single-cell definition at ``path`` (coordinate + shared knobs).

    The YAML carries the shared knobs (lengths, SLO, seed) and the one coordinate the
    cell runs (prefix_share, burstiness, optional max_concurrency); the execution context is
    injected from the CLI. The coordinate is range-checked as the ``CellConfig`` fields
    validate. See :func:`_load_config`.

    :param path: the config YAML file, e.g. bench/load-cell.yaml.
    :param base_url: the endpoint the cell targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for this cell's result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param tokenizer: a prompt-synthesis tokenizer to bind (the baked snapshot path on
        the self-hosted arm); None leaves the YAML value intact.
    :return: the validated cell config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    return _load_config(
        path,
        CellConfig,
        label="cell config",
        base_url=base_url,
        model=model,
        out_dir=out_dir,
        commercial=commercial,
        tokenizer=tokenizer,
    )
