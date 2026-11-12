from .base import Kontekst, Loader, Resultat, boelge_filter, formatér_payload
from .dokument import DokumentLoader
from .indsats import IndsatsLoader
from .notat import NotatLoader
from .sag import SagLoader

LOADERS = {
    "sag": SagLoader,
    "dokument": DokumentLoader,
    "notat": NotatLoader,
    "indsats": IndsatsLoader,
    # "part": PartLoader,    # tilføjes når partstrukturen i GO er afdækket
}

__all__ = [
    "Kontekst",
    "Loader",
    "Resultat",
    "SagLoader",
    "DokumentLoader",
    "NotatLoader",
    "IndsatsLoader",
    "LOADERS",
    "boelge_filter",
    "formatér_payload",
]
