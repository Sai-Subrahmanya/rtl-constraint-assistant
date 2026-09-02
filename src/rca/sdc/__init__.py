from .base import SDCBackend
from .cadence import CadenceSDCBackend
from .generic import GenericSDCBackend
from .opensta import OpenSTASDCBackend
from .parser import SDCParser
from .synopsys import SynopsysSDCBackend


def get_backend(name: str) -> SDCBackend:
    backends = {
        "generic": GenericSDCBackend,
        "opensta": OpenSTASDCBackend,
        "synopsys": SynopsysSDCBackend,
        "cadence": CadenceSDCBackend,
    }
    cls = backends.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown SDC backend '{name}'. Available: {list(backends)}")
    return cls()


__all__ = [
    "SDCBackend", "SDCParser", "get_backend",
    "GenericSDCBackend", "OpenSTASDCBackend",
    "SynopsysSDCBackend", "CadenceSDCBackend",
]
