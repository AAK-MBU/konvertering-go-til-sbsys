"""Sammensætning: bygger en kørselsklar Kontekst ud fra Settings.

Ét sted hvor alle afhængigheder kobles sammen. Det gør det trivielt at
bytte SBSYS-klienten ud med en mock i test, uden at loaderne ved noget.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .files import FilOpslag
from .hashing import maskér
from .idmap import IdMap
from .loaders.base import Kontekst
from .mapping import Kodelister
from .sbsys.client import SbsysKlient
from .sbsys.endpoints import Endpoints

log = logging.getLogger(__name__)


class MaskerendeFilter(logging.Filter):
    """Sidste værn mod CPR-numre i logfiler.

    Automation Server opsamler stdout og Pythons logging og gemmer det på
    sessionen. Uden dette filter ville en tilfældig exception-tekst med et
    CPR-nummer ende i en driftsdatabase.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = maskér(record.msg)
        if record.args:
            record.args = tuple(
                maskér(a) if isinstance(a, str) else a for a in _som_tuple(record.args)
            )
        return True


def _som_tuple(args: Any) -> tuple:
    return args if isinstance(args, tuple) else (args,)


def opsaet_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s")
    )
    if settings.maskér_persondata:
        handler.addFilter(MaskerendeFilter())
    rod = logging.getLogger()
    rod.handlers = [handler]
    rod.setLevel(settings.log_niveau.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def byg_kontekst(
    settings: Settings, run_id: int | None = None, boelge: str | None = None,
    vis_payload: bool = False,
) -> Kontekst:
    db = Database(settings.db)

    def kald_logger(**kwargs: Any) -> None:
        kontekst = kwargs.pop("kontekst", {}) or {}
        db.udfoer(
            """
            INSERT INTO konv.api_kald
                (run_id, korrelations_id, objekt_type, kilde_noegle, operation,
                 metode, sti, http_status, varighed_ms, forsoeg_nr, fejl_tekst)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_id,
            kwargs["korrelations_id"],
            kontekst.get("objekt_type"),
            kontekst.get("kilde_noegle"),
            kwargs["operation"],
            kwargs["metode"],
            kwargs["sti"],
            kwargs["http_status"],
            kwargs["varighed_ms"],
            kwargs["forsoeg_nr"],
            maskér(kwargs.get("fejl"))[:2000] if kwargs.get("fejl") else None,
        )

    klient = SbsysKlient(
        settings.sbsys,
        kald_logger=kald_logger,
        korrelations_praefiks=settings.korrelations_praefiks,
    )

    # Børn og Unge-API'et: samme token og samme indstillinger, anden vært.
    bu_klient = None
    if settings.sbsys.bu_base_url:
        bu_klient = SbsysKlient(
            replace(settings.sbsys, base_url=settings.sbsys.bu_base_url),
            kald_logger=kald_logger,
            korrelations_praefiks=settings.korrelations_praefiks,
        )

    endpoints_sti = Path(os.environ["KONV_ENDPOINTS"]) if os.environ.get("KONV_ENDPOINTS") else None

    return Kontekst(
        settings=settings,
        db=db,
        idmap=IdMap(db),
        klient=klient,
        endpoints=Endpoints.fra_fil(endpoints_sti),
        kodelister=Kodelister.fra_db(db),
        filer=FilOpslag(settings.filer),
        bu_klient=bu_klient,
        run_id=run_id,
        boelge=boelge,
        vis_payload=vis_payload,
    )
