"""Hashing og maskering.

``payload_hash`` er det der afgør om et objekt skal genindlæses efter en ny
bacpac-leverance: samme nøgle + samme hash = ingenting at gøre.
Derfor SKAL hashen være stabil på tværs af processer og Python-versioner —
ingen ``hash()``, ingen dict-rækkefølge, ingen tidsstempler i payloaden.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_CPR = re.compile(r"\b(\d{6})[- ]?(\d{4})\b")
_CVR = re.compile(r"\b(\d{8})\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _normaliser(vaerdi: Any) -> Any:
    """Gør værdien JSON-serialiserbar og deterministisk."""
    if isinstance(vaerdi, (datetime, date)):
        return vaerdi.isoformat()
    if isinstance(vaerdi, Decimal):
        return format(vaerdi, "f")
    if isinstance(vaerdi, Path):
        return vaerdi.as_posix()
    if isinstance(vaerdi, bytes):
        return hashlib.sha256(vaerdi).hexdigest()
    if isinstance(vaerdi, dict):
        return {str(k): _normaliser(v) for k, v in sorted(vaerdi.items(), key=lambda p: str(p[0]))}
    if isinstance(vaerdi, (list, tuple)):
        return [_normaliser(v) for v in vaerdi]
    if isinstance(vaerdi, set):
        return sorted(_normaliser(v) for v in vaerdi)
    return vaerdi


def payload_hash(payload: Any) -> str:
    """Stabil SHA-256 af en payload. Samme input -> altid samme streng."""
    kanonisk = json.dumps(
        _normaliser(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(kanonisk.encode("utf-8")).hexdigest()


def fil_sha256(sti: Path, blok: int = 1024 * 1024) -> str:
    """Checksum af en dokumentfil. Bruges til at bevise at bytes kom uændret over."""
    h = hashlib.sha256()
    with sti.open("rb") as f:
        for stykke in iter(lambda: f.read(blok), b""):
            h.update(stykke)
    return h.hexdigest()


def maskér(tekst: str | None) -> str:
    """Fjern CPR, CVR og e-mail fra tekst der skal i en logfil.

    Konverteringen håndterer børne- og ungesager. Logfiler ender i
    Automation Server og i drift-værktøjer, og der må der ikke ligge
    personhenførbare oplysninger.
    """
    if not tekst:
        return ""
    ud = _CPR.sub(lambda m: f"{m.group(1)}-XXXX", tekst)
    ud = _EMAIL.sub("<email>", ud)
    ud = _CVR.sub("<cvr>", ud)
    return ud


def kort_noegle(kilde_noegle: str, laengde: int = 12) -> str:
    """Kort, ikke-reversibel reference til brug i logs når nøglen i sig selv
    kan være personhenførbar (fx hvis GO har brugt CPR i nøglen)."""
    return hashlib.sha256(kilde_noegle.encode("utf-8")).hexdigest()[:laengde]
