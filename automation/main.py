"""Automation Server-indgang.

Mønsteret er det samme som i odense-rpa/process-template: ét kald med
``--queue`` fylder køen, og kørsler uden ``--queue`` tømmer den.

Ét workitem = ét objekt (én sag, ét dokument). Det er bevidst:

* et dokument der fejler blokerer ikke de 40.000 andre
* Automation Servers indbyggede "pending user" bliver arbejdslisten for
  fagpersonerne — de kan se præcis hvilke sager der kræver stillingtagen
* genkørsel af ét objekt er ét klik i web-fladen

Registrér én proces pr. objekttype i Automation Server, alle med samme
git-repo, og styr forskellen via trigger-parametrene:

    Proces "Konv – sager"      parametre:  --fase sag --boelge ppr
    Proces "Konv – sager (kø)" parametre:  --fase sag --boelge ppr --queue
    Proces "Konv – dokumenter" parametre:  --fase dokument --boelge ppr

Bølgen sættes på triggeren, ikke i koden. Så kan den samme proces bruges
til alle ti bølger ved at rette ét felt i web-fladen — og hvilken bølge en
session kørte, står i dens parametre bagefter.

Rækkefølgen er en hård afhængighed: sager skal være indlæst før dokumenter,
parter og notater. Kør dem som separate trin, ikke parallelt.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from automation_server_client import (  # type: ignore[import-not-found]
    AutomationServer,
    Credential,
    WorkItemError,
    Workqueue,
)

from konvertering import boelge
from konvertering.app import byg_kontekst, opsaet_logging
from konvertering.config import Settings
from konvertering.db import Database
from konvertering.idmap import afslut_run, opret_run
from konvertering.loaders import LOADERS
from konvertering.sbsys.errors import AutentifikationsFejl, PermanentFejl

log = logging.getLogger(__name__)

TABELLER = {
    "sag": "stg.v_sager_til_indlaesning",
    "dokument": "stg.v_dokumenter_til_indlaesning",
}


def _credential(ats: AutomationServer, navn: str) -> Credential | None:
    """Hent en credential, eller None hvis den ikke findes.

    Adskilt, fordi en manglende credential skal give en besked der siger
    HVILKEN der mangler. Et opslag der kaster midt i opstarten efterlader
    kun et stacktrace fra klientbiblioteket.
    """
    try:
        return Credential.get_by_name(ats, navn)
    except Exception as e:  # noqa: BLE001 — klientbiblioteket er ikke specifik
        log.warning("Credential '%s' kunne ikke hentes: %s", navn, e)
        return None


def _hent_credentials(ats: AutomationServer) -> None:
    """Hemmeligheder kommer fra Automation Servers credential-store og
    lægges i environment, så resten af koden kun kender Settings.

    SBSYS kræver TO sæt: klienten (integrationen) og brugeren (hvem
    handlingerne udføres som). De ligger i hver sin credential, fordi de
    skiftes uafhængigt af hinanden — en adgangskode der udløber hvert
    kvartal må ikke tvinge integrationsnøglen til at blive rørt.

        sbsys-api     username = client_id,  password = client_secret
        sbsys-bruger  username = SBSYS-bruger, password = adgangskode
    """
    klient = _credential(ats, "sbsys-api")
    if klient:
        os.environ.setdefault("SBSYS_CLIENT_ID", klient.username or "")
        os.environ.setdefault("SBSYS_CLIENT_SECRET", klient.password or "")
        for noegle, vaerdi in (klient.data or {}).items():
            os.environ.setdefault(noegle, str(vaerdi))

    bruger = _credential(ats, "sbsys-bruger")
    if bruger and bruger.username:
        os.environ.setdefault("SBSYS_USERNAME", bruger.username)
        os.environ.setdefault("SBSYS_PASSWORD", bruger.password or "")
    else:
        log.warning(
            "Ingen credential 'sbsys-bruger'. SBSYS' snitflade kræver en "
            "navngiven bruger; uden den svarer API'et 401. Opret den med "
            "username = SBSYS-brugernavn og password = adgangskode."
        )

    db = Credential.get_by_name(ats, "konv-database")
    if db and db.username:
        os.environ.setdefault("KONV_DB_TRUSTED", "0")
        os.environ.setdefault("KONV_DB_BRUGER", db.username)
        os.environ.setdefault("KONV_DB_KODEORD", db.password or "")
        for noegle, vaerdi in (db.data or {}).items():
            os.environ.setdefault(noegle, str(vaerdi))


async def populate_queue(
    workqueue: Workqueue, fase: str, settings: Settings, valgt_boelge: str | None
) -> None:
    """Læg ét workitem pr. objekt der mangler indlæsning.

    ``reference`` sættes til bølge + kilde-nøgle. Automation Server viser den
    i web-fladen, så et fejlet item kan slås direkte op i GO og i konv.id_map
    — og man kan se hvilken bølge det hørte til uden at åbne det.
    """
    db = Database(settings.db)
    valgt_boelge = boelge.valider(db, valgt_boelge)
    view = TABELLER[fase]

    # Sideopdelt frem for streamet: koeen kan have hundredtusinder af
    # objekter, og en cursor der staar aaben gennem lige saa mange HTTP-kald
    # til Automation Server er unoedigt skroebelig.
    filter_sql = " AND boelge = ?" if valgt_boelge else ""
    raekker = db.hent_i_sider(
        view, filter_sql, parametre=(valgt_boelge,) if valgt_boelge else ()
    )

    antal = 0
    for raekke in raekker:
        workqueue.add_item(
            data={
                "fase": fase,
                "kilde_noegle": raekke["kilde_noegle"],
                "boelge": raekke.get("boelge"),
            },
            reference=f"{raekke.get('boelge') or '-'}/{raekke['kilde_noegle']}"[:100],
        )
        antal += 1

    log.info("Lagde %d %s-objekter i køen (bølge: %s)", antal, fase, valgt_boelge or "alle")
    if antal == 0 and valgt_boelge:
        # En tom kø ser ud som en vellykket kørsel i enhver rapport.
        raise boelge.TomBoelge(
            f"Bølgen '{valgt_boelge}' har ingen {fase}-objekter. "
            f"Tjek bølgetildelingen og stg.v_boelge_mangler."
        )
    db.luk()


async def process_workqueue(
    workqueue: Workqueue, fase: str, settings: Settings, valgt_boelge: str | None
) -> None:
    db = Database(settings.db)
    valgt_boelge = boelge.valider(db, valgt_boelge)
    run_id = opret_run(
        db, fase="load", miljoe=settings.miljoe, dry_run=settings.dry_run,
        objekt_type=fase, kodeversion=settings.kodeversion,
        as_session_id=os.environ.get("ATS_SESSION_ID"),
        boelge=valgt_boelge, maal=settings.sbsys.base_url,
    )
    kontekst = byg_kontekst(settings, run_id, valgt_boelge)
    loader = LOADERS[fase](kontekst)
    indlaest = fejlet = 0

    try:
        for item in workqueue:
            with item:
                kilde_noegle = item.data.get("kilde_noegle")
                if not kilde_noegle:
                    raise WorkItemError("workitem mangler kilde_noegle")
                try:
                    resultat = loader.behandl_noegle(str(kilde_noegle))
                except AutentifikationsFejl:
                    # Servicekontoen er nede. Stop hele sessionen frem for at
                    # brænde resten af køen af på samme fejl.
                    raise
                except PermanentFejl as e:
                    # Data er forkerte — en fagperson skal se på det.
                    raise WorkItemError(str(e)) from e

                if resultat.indlaest:
                    indlaest += 1
                    item.complete("Indlæst i SBSYS")
                elif resultat.afvist or resultat.fejlet:
                    fejlet += 1
                    item.pending_user("Kræver stillingtagen — se konv.afvist")
                else:
                    item.complete("Sprunget over (allerede indlæst eller dry-run)")

        afslut_run(db, run_id, "ok", f"indlæst={indlaest} fejlet={fejlet}")
    except Exception as e:
        afslut_run(db, run_id, "fejlet", str(e)[:1000])
        raise
    finally:
        kontekst.klient.luk()
        db.luk()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--fase", required=True, choices=sorted(TABELLER))
    p.add_argument("--boelge", default=None, help="Bølgekode, fx ppr eller dagtilbud-4")
    p.add_argument("--queue", action="store_true", help="Fyld køen i stedet for at tømme den")
    args, _ = p.parse_known_args()

    ats = AutomationServer.from_environment()
    _hent_credentials(ats)

    settings = Settings.fra_environment()
    opsaet_logging(settings)
    workqueue = ats.workqueue()

    if args.queue:
        asyncio.run(populate_queue(workqueue, args.fase, settings, args.boelge))
        return

    asyncio.run(process_workqueue(workqueue, args.fase, settings, args.boelge))


if __name__ == "__main__":
    main()
    sys.exit(0)
