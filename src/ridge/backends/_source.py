"""Load packaged helper source without executing it in the Ridge host."""

from importlib.resources import files

_SCRIPTS = files("ridge.backends").joinpath("_scripts")
_ROOTS_SOURCE = _SCRIPTS.joinpath("roots.py").read_text(encoding="utf-8") + "\n"
HELPER_SOURCE = (
    _ROOTS_SOURCE
    + _SCRIPTS.joinpath("deletion.py").read_text(encoding="utf-8")
    + "\n"
    + _SCRIPTS.joinpath("operations.py").read_text(encoding="utf-8")
)
TRANSFER_HELPER_SOURCE = _ROOTS_SOURCE + _SCRIPTS.joinpath("transfer.py").read_text(
    encoding="utf-8"
)
