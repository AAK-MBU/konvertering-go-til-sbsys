"""Dokumentloader — ét kald: metadata og fil på én gang.

SBSYS journaliserer et dokument i ét multipart-kald, hvor filen og dens
metadata følges ad. Det er en bedre verden end to kald: der kan ikke
opstå en tom dokumentskal i SBSYS, fordi uploaden fejlede efter metadata
var oprettet.

Til gengæld er kaldet ikke idempotent. Går svaret tabt undervejs, er
dokumentet måske oprettet alligevel, og et genforsøg giver en dublet.
Derfor tjekkes filen FØR kaldet, og derfor er den eksterne nøgle lige så
afgørende her som på sagen — se adr/ADR-0001-idempotens.md og A1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from ..files import FILSTATUS_OK
from ..models import Dokument, Objekttype, Retning
from ..sbsys.errors import FilFejl, PermanentFejl
from .base import Loader, boelge_filter

log = logging.getLogger(__name__)


class DokumentLoader(Loader[Dokument]):
    arbejdsliste = "stg.v_dokumenter_til_indlaesning"
    objekt_type = Objekttype.DOKUMENT

    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        filter_sql, params = boelge_filter(self.k.boelge)
        return self.k.db.hent_i_sider(
            self.arbejdsliste,
            filter_sql,
            parametre=params,
            graense=graense,
        )

    def byg(self, raekke: dict[str, Any]) -> Dokument:
        m = self.k.kodelister
        retning = (raekke.get("retning") or "internt").lower()
        return Dokument(
            kilde_noegle=str(raekke["kilde_noegle"]),
            sag_noegle=str(raekke["sag_noegle"]),
            titel=(raekke.get("titel") or raekke.get("fil_navn") or "").strip() or "Uden titel",
            kategori=m.slaa_op("dokumentkategori", raekke.get("kategori_go"), paakraevet=False),
            dokument_dato=raekke.get("dokument_dato"),
            registreret=raekke.get("registreret"),
            forfatter=m.slaa_op("bruger", raekke.get("forfatter_go"), paakraevet=False),
            retning=Retning(retning) if retning in Retning.__members__.values() else Retning.INTERNT,
            version=raekke.get("version"),
            er_kladde=bool(raekke.get("er_kladde")),
            fil_sti=raekke.get("fil_relativ_sti"),
            fil_navn=raekke.get("fil_navn"),
            fil_stoerrelse=raekke.get("fil_stoerrelse"),
            fil_sha256=raekke.get("fil_sha256"),
            mime=raekke.get("mime"),
        )

    def payload(self, objekt: Dokument) -> dict[str, Any]:
        if objekt.er_kladde:
            # GO's Journaliseret = 0. Et dokument paa en sag i SBSYS er
            # enten journaliseret eller en kladde, og journalisering er
            # ikke en teknisk detalje: den aendrer dokumentets status i en
            # boernesag og kan ikke rulles tilbage. Derfor sin egen sti,
            # POST /api/kladde, og sin egen kropsform.
            navn = objekt.fil_navn or objekt.titel
            _, _, endelse = navn.rpartition(".")
            return self.k.endpoints.byg_payload(
                "kladde",
                {
                    "titel": objekt.titel,
                    "filnavn": navn,
                    # Filendelsen staar for sig i KladdeRedigerDto. Uden
                    # den ved SBSYS ikke hvilket program der skal aabne
                    # kladden, og den bliver ubrugelig for sagsbehandleren.
                    "filendelse": endelse.lower() or None,
                },
            )
        return self.k.endpoints.byg_payload(
            "dokument",
            {
                "titel": objekt.titel,
                "kategori": objekt.kategori,
                "dokument_dato": objekt.dokument_dato.isoformat() if objekt.dokument_dato else None,
                # Ingen retning: JournaliserDokumentInputDtoV10 har intet felt
                # til den, og GO har heller ikke et ind-/udgaaende-begreb (A21).
                "forfatter": objekt.forfatter,
                # Ingen ekstern_noegle: besluttet 11-11-2026 at GO-nøglen IKKE
                # skrives ind i SBSYS, heller ikke hvor feltet findes.
                # Journaliseringens "Eksternid" står stadig i endpoints.py,
                # fordi det er en egenskab ved snitfladen — men det udfyldes
                # ikke. Koblingen lever kun i konv.id_map.
            },
        )

    def send(self, objekt: Dokument, payload: dict[str, Any]) -> tuple[str, str | None]:
        sbsys_sag_id = self.k.idmap.sbsys_id(Objekttype.SAG, objekt.sag_noegle)
        if not sbsys_sag_id:
            raise PermanentFejl(
                f"Sagen {objekt.sag_noegle[:12]} er ikke indlæst endnu — "
                f"dokumentet kan ikke placeres"
            )

        # Filen findes og verificeres FØR metadata oprettes, så vi ikke
        # efterlader tomme dokumentskaller i SBSYS.
        resultat = self.k.filer.undersoeg(objekt.fil_sti, beregn_checksum=False)
        if resultat.status != FILSTATUS_OK or resultat.sti is None:
            raise FilFejl(resultat.besked or "Filen kunne ikke bruges",
                          regel=f"fil_{resultat.status}", sti=objekt.fil_sti)

        # Metadata og fil sendes i ÉT multipart-kald. Det er ikke en
        # forenkling af to kald — SBSYS har ikke de to kald. Filen ligger i
        # "file", metadata som JSON-streng i formfeltet "json".
        #
        # At det er ét kald fjerner samtidig hele klassen af fejl hvor der
        # står en tom dokumentskal tilbage i SBSYS, fordi uploaden fejlede
        # efter metadata var oprettet.
        indhold = self.k.filer.laes(resultat.sti)
        metadata = {"SagID": sbsys_sag_id, **payload}
        navn = "opret_kladde" if objekt.er_kladde else "journaliser"
        op = self.k.endpoints.operation(navn)
        svar = self.k.klient.kald(
            op.metode, op.sti, operation=navn,
            filer={"file": (objekt.fil_navn or resultat.sti.name, indhold,
                            objekt.mime or resultat.mime or "application/octet-stream")},
            data={"json": json.dumps(metadata, ensure_ascii=False)},
            forventede_status=op.forventede_status,
            kontekst={"objekt_type": "dokument", "kilde_noegle": objekt.kilde_noegle},
        )
        krop = svar.krop or {}
        # Både kladder og dokumenter har TO id'er: registreringens (på denne
        # sag) og det underliggende objekts. SBSYS arbejder med
        # registreringens — GET/PUT/DELETE, arkivering, journalisering og
        # delforløb tager alle et registrerings-id. Bekræftet i testmiljøet
        # 17-11-2026:
        #   kladde:   ID = 183 (GET /api/kladde/183 finder den),
        #             KladdeID = 199 (GET /api/kladde/199 giver 404).
        #             At ID også var sagens id (183), var et tilfælde.
        #   dokument: Id = 185 (registreringen), DokumentID = 187.
        # Ingen fallback til KladdeID/DokumentID: det ville gemme et id der
        # ligner det rigtige, men ikke kan bruges til noget.
        felter = ("ID",) if objekt.er_kladde else ("Id", "id")
        dokument_id = next((krop[f] for f in felter if krop.get(f)), None)
        if not dokument_id:
            raise PermanentFejl(
                f"SBSYS returnerede intet id fra {navn}",
                korrelations_id=svar.korrelations_id,
            )
        return str(dokument_id), None
