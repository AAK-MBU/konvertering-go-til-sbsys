"""Journalnotater.

SBSYS kalder dem journalarknoter, og de oprettes med ét kald:
``POST /api/journalarknote/create`` med ``SagID``, ``Overskrift``, ``Note``
og ``KontaktTidspunkt``.

To ting er værd at vide, før der konverteres for alvor:

**SBSYS renderer HTML i notefeltet.** Det er sådan tabeller og formatering
kommer ind. Kommer teksten fra GO som ren tekst, skal den escapes — ellers
bliver et ``<`` i en adresse eller et navn til begyndelsen på et tag, og
notatet kommer til at se forkert ud for sagsbehandleren. Vi escaper derfor
som standard; indeholder GO's tekst allerede HTML, slås det fra i
``endpoints.toml`` med ``notat_er_html = true``.

**Om GO's journalark overhovedet skal blive til notater er ikke afklaret.**
Alternativet er ét dokument pr. sag med hele journalarket samlet. Begge
dele er mulige, og valget er fagligt — se spørgsmål B-rækken og
docs/04-aabne-spoergsmaal.md. Loaderen her findes, så muligheden kan
afprøves, ikke fordi beslutningen er truffet.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..models import Notat, Objekttype
from ..sbsys.errors import PermanentFejl, ValideringsFejl
from .base import Loader, boelge_filter

log = logging.getLogger(__name__)


class NotatLoader(Loader[Notat]):
    arbejdsliste = "stg.v_notater_til_indlaesning"
    objekt_type = Objekttype.NOTAT

    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        filter_sql, params = boelge_filter(self.k.boelge)
        return self.k.db.hent_i_sider(
            self.arbejdsliste,
            filter_sql,
            parametre=params,
            graense=graense,
        )

    def byg(self, raekke: dict[str, Any]) -> Notat:
        m = self.k.kodelister
        return Notat(
            kilde_noegle=str(raekke["kilde_noegle"]),
            sag_noegle=str(raekke["sag_noegle"]),
            titel=(raekke.get("titel") or "").strip() or "Journalnotat",
            tekst=raekke.get("tekst"),
            forfatter=m.slaa_op("bruger", raekke.get("forfatter_go"), paakraevet=False),
            dato=raekke.get("dato"),
            notat_type=m.slaa_op("notattype", raekke.get("notat_type_go"), paakraevet=False),
        )

    def payload(self, objekt: Notat) -> dict[str, Any]:
        return self.k.endpoints.byg_payload(
            "notat",
            {
                "titel": objekt.titel,
                "tekst": self._tekst(objekt),
                # KontaktTidspunkt er obligatorisk. Notatets egen dato er den
                # rigtige — ikke dagens, som ville udviske sagens tidslinje.
                "dato": (objekt.dato or datetime.now(UTC)).isoformat(),
            },
        )

    def _tekst(self, objekt: Notat) -> str:
        """Notetekst klar til SBSYS, som renderer feltet som HTML."""
        raa = objekt.tekst or ""
        if self.k.endpoints.notat_er_html:
            return raa
        # Linjeskift bevares: uden <br> bliver et flersidet notat til én
        # lang blok, og det er ulæseligt for den der skal bruge det.
        # GO's tekst har Windows-linjeskift (\r\n). Kun \n blev erstattet før,
        # så \r blev hængende foran hvert <br> (dry-run 17-11-2026).
        ensartet = raa.replace("\r\n", "\n").replace("\r", "\n")
        return html.escape(ensartet).replace("\n", "<br>")

    def send(self, objekt: Notat, payload: dict[str, Any]) -> tuple[str, str | None]:
        sbsys_sag_id = self.k.idmap.sbsys_id(Objekttype.SAG, objekt.sag_noegle)
        if not sbsys_sag_id:
            raise ValideringsFejl(
                f"Sagen {objekt.sag_noegle[:12]} er ikke indlæst endnu — "
                f"notatet kan ikke placeres",
                regel="sag_ikke_indlaest", felt="sag_noegle",
            )

        krop = {self.k.endpoints.felt("notat", "sag_id"): int(sbsys_sag_id), **payload}
        op = self.k.endpoints.operation("opret_notat")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation="opret_notat", json=krop,
            forventede_status=op.forventede_status,
            kontekst={"objekt_type": "notat", "kilde_noegle": objekt.kilde_noegle},
        )
        legeme = svar.krop or {}
        notat_id = legeme.get("Id") or legeme.get("id")
        if not notat_id:
            raise PermanentFejl(
                "SBSYS returnerede intet notat-id", korrelations_id=svar.korrelations_id
            )
        return str(notat_id), None
