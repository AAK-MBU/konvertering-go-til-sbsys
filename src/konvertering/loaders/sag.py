"""Sagsloader — den første og vigtigste. Alt andet hænger på en sag."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

from ..hashing import kort_noegle, maskér
from ..models import Objekttype, Sag
from ..sbsys.errors import (
    CertifikatFejl,
    ForbigaaendeFejl,
    PermanentFejl,
    VaertsnavnFejl,
    ValideringsFejl,
)
from .base import Loader, boelge_filter
from .brugere import BrugerOpslag, find_az

TOM_AFDELING = "<tom>"
# Tvinger ÉN sagsbehandler (AZ-ident) på alle sager. Kun til test: PPR-sager
# er beskyttede, og den der tester, skal kunne se sagen i brugerfladen.
TVUNGEN_SAGSBEHANDLER = "TEST_SAGSBEHANDLER_AZ"


def afdeling_noegle(raekke: dict[str, Any]) -> str:
    """Nøglen i kodelisten ``afdeling`` for sagens ansættelsessted.

    GO's ``Cases.Afdeling`` er NULL på alle PPR-sager i dumpet (20-11-2026).
    En tom afdeling slås derfor op som ``<tom>|<sagstype>`` — fx
    ``<tom>|ABA`` — så ansættelsesstedet kan vælges pr. sagstype i
    mapping/afdeling.csv. Mangler rækken, afvises sagen som før: på
    PPR-skabelonerne styrer ansættelsesstedet adgangen til sagen
    (AnvendSikkerhedFraAnsaettelsessted), så det må aldrig gættes.
    """
    afdeling = (raekke.get("afdeling_go") or "").strip()
    if afdeling:
        return afdeling
    return f"{TOM_AFDELING}|{(raekke.get('sagstype_go') or '').strip()}"

log = logging.getLogger(__name__)

# Organiseringer hvor ansættelsesstedet IKKE sendes: SBSYS sætter det selv ud
# fra sagsbehandleren (set i prod 27-11-2026: et skift af sagsbehandler på en
# PPR-sag skiftede ansættelsesstedet med). Gælder også PPR fra 27-11-2026.
ARVER_ANSAETTELSESSTED = frozenset({"PPR", "EMN", "BOR"})
# Organiseringer hvor en sag uden part er i orden: emnesagernes skabeloner
# kræver ingen part (PartForlangAltid = False, prod 27-11-2026).
PART_VALGFRI = frozenset({"EMN"})


def sagskommentar(go_sagsnummer: str | None, startdato: Any) -> str | None:
    """Sagens Kommentar i SBSYS: GO's sags-id og, under det, GO's startdato
    (besluttet 27-11-2026, alle sagstyper). Startdatoen kan ikke sættes i
    SBSYS' egne datofelter — Opstaaet og Oprettet ignoreres af API'et."""
    linjer = [str(go_sagsnummer).strip()] if go_sagsnummer and str(go_sagsnummer).strip() else []
    if hasattr(startdato, "strftime"):
        linjer.append(f"Startdato i GO: {startdato.strftime('%d-%m-%Y')}")
    return "\n".join(linjer) or None


def _samme_tekst(a: Any, b: str) -> bool:
    """Ens tekst, når linjeskift (\\r\\n / \\n) og kantmellemrum ses bort fra."""
    def norm(t: Any) -> str:
        return "\n".join(linje.strip() for linje in str(t or "").strip().splitlines())
    return norm(a) == norm(b)


def organisering(raekke: dict[str, Any]) -> str:
    return (raekke.get("organisering_go") or "").strip().upper()


def skabelon_noegle(raekke: dict[str, Any], kodelister: Any) -> str | None:
    """Nøglen i kodelisten ``sagsskabelon``: ``<sagsprofil>|<organisering>``
    hvis den række findes, ellers sagsprofilen alene.

    Nogle sagsprofiler bruges både som borger- og emnesag (fx "MBU DT Klage")
    og har en skabelon for hver. De står som "MBU DT Klage|BOR" og
    "MBU DT Klage|EMN"; alle andre som profilen alene."""
    profil = (raekke.get("sagstype_go") or "").strip()
    if not profil:
        return None
    saerlig = f"{profil}|{organisering(raekke)}"
    return saerlig if kodelister.findes("sagsskabelon", saerlig) else profil


def sagstitel(raekke: dict[str, Any]) -> str:
    """PPR-sager får sagsprofilen som titel (Psykolog, ABA ...), uanset hvad
    GO's sagsnavn er — det er ofte barnets navn (besluttet 26-11-2026).
    Emnesager, elevmapper og borgersager: GO's sagsnavn 1:1."""
    if (raekke.get("organisering_go") or "").strip().upper() == "PPR":
        profil = (raekke.get("sagstype_go") or "").strip()
        if profil:
            return profil
    return (raekke.get("titel") or "").strip() or "Uden titel"


class SagLoader(Loader[Sag]):
    arbejdsliste = "stg.v_sager_til_indlaesning"
    objekt_type = Objekttype.SAG

    def __init__(self, k) -> None:  # noqa: ANN001
        super().__init__(k)
        self._part_cache: dict[str, int] = {}
        # kilde_noegle -> (CPR/CVR formateret til SBSYS, er_firma). Til genfindingen.
        self._part_nummer: dict[str, tuple[str, bool]] = {}
        self._brugere = BrugerOpslag(k)
        self._tvungen_advaret = False

    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        # Sideopdelt efter nøgle, ikke streamet: loaderen skriver til
        # konv.id_map undervejs, og et commit dræber en åben cursor.
        filter_sql, params = boelge_filter(self.k.boelge)
        return self.k.db.hent_i_sider(
            self.arbejdsliste,
            filter_sql,
            parametre=params,
            graense=graense,
        )

    def byg(self, raekke: dict[str, Any]) -> Sag:
        m = self.k.kodelister
        return Sag(
            kilde_noegle=str(raekke["kilde_noegle"]),
            titel=sagstitel(raekke),
            go_sagsnummer=raekke.get("go_sagsnummer"),
            beskrivelse=raekke.get("beskrivelse"),
            # KLE og facet sendes IKKE: alle sager oprettes fra en
            # sagsskabelon, og skabelonen sætter selv KLE og facet (besluttet
            # 26-11-2026 — gælder PPR og de senere emnesager, borgersager og
            # elevmapper). GO's værdier står stadig i stg.sag (kle_go, facet_go).
            kle=None,
            facet=None,
            sagstype=m.slaa_op("sagstype", raekke.get("sagstype_go"), paakraevet=False),
            # Skabelonen er blokerende: SBSYS kan ikke oprette en sag uden.
            skabelon=m.slaa_op("sagsskabelon", skabelon_noegle(raekke, m), paakraevet=True),
            status=m.slaa_op("sagsstatus", raekke.get("status_go"), paakraevet=False),
            # Ukendt sagsbehandler er ikke blokerende: sager fra medarbejdere
            # der er fratrådt skal stadig med over. De havner på en fælles
            # konto via 'standard'-handlingen i kodelisten.
            ansvarlig=self._ansvarlig(raekke),
            afdeling=(None if organisering(raekke) in ARVER_ANSAETTELSESSTED
                      else m.slaa_op("afdeling", afdeling_noegle(raekke), paakraevet=True)),
            oprettet_dato=raekke.get("oprettet_dato"),
            afsluttet_dato=raekke.get("afsluttet_dato"),
            fortrolighed=m.slaa_op("fortrolighed", raekke.get("fortrolighed_go"), paakraevet=False),
            bevaring=m.slaa_op("bevaring", raekke.get("bevaring_go"), paakraevet=False),
            primaer_part_noegle=raekke.get("primaer_part_noegle"),
            part_valgfri=organisering(raekke) in PART_VALGFRI,
        )

    def payload(self, objekt: Sag) -> dict[str, Any]:
        """Kroppen til POST /api/sag/template — plus det der saettes bagefter.

        Kun felter CreateSagFromSkabelonDto kender, sendes med. Fire ting fra
        GO kan IKKE gaa med i oprettelsen:

        * **status** saettes i et selvstaendigt kald efter oprettelsen. Den
          ligger under ``_efter_oprettelse`` i payloaden, saa den ses i
          ``--vis-payload`` og indgaar i hashen, men den sendes ikke med i
          oprettelsen. ``send()`` tager den ud.
        * **sagstypen** kommer fra skabelonen.
        * **oprettelses- og afslutningsdato** findes ikke paa modellen —
          spoergsmaal A2. De ligger stadig i stg.sag.
        * **beskrivelsen** findes heller ikke paa modellen.
        """
        e = self.k.endpoints
        krop = e.byg_payload(
            "sag",
            {
                "titel": objekt.titel,
                "kle": objekt.kle,
                "facet": objekt.facet,
                "skabelon": objekt.skabelon,
                "ansvarlig": objekt.ansvarlig,
                "afdeling": objekt.afdeling,
                # Ingen ekstern_noegle: besluttet 11-11-2026 at GO-nøglen IKKE
                # skrives ind i SBSYS. Koblingen lever kun i konv.id_map.
                # Se ADR-0001 og _find_identisk nedenfor for følgen.
            },
        )
        # Parten bygges HER og ikke i send(), selv om det koster et opslag i
        # dry-run: ellers viser --vis-payload en payload der ikke er den der
        # sendes, og så er gennemsynet før første rigtige kald værdiløst.
        # Opslaget er read-only.
        if objekt.primaer_part_noegle or not objekt.part_valgfri:
            part = self._part_reference(objekt)
            krop[e.felt("sag", "primaer_part")] = part
            krop[e.felt("sag", "parter")] = [part]

        efter: dict[str, Any] = {}
        # Sagens Kommentar står ikke på CreateSagFromSkabelonDto i spec'en,
        # kun på SagDtoV10 (PUT /api/sag/{id}). FORSØG (27-11-2026): den sendes
        # alligevel med i oprettelsen. Tager SBSYS imod den, springer
        # _saet_kommentar PUT'en over og logger det; ellers sætter PUT'en den.
        kommentar = sagskommentar(objekt.go_sagsnummer, objekt.oprettet_dato)
        if kommentar:
            krop["Kommentar"] = kommentar
            efter["kommentar"] = kommentar
        if objekt.status:
            # Kommentaren sendes altid: en status kan have RequireComments
            # (SagsStatusDto), og saa afviser SBSYS skiftet uden. Passiv er
            # en oplagt kandidat. Teksten er ikke en GO-noegle — den siger
            # kun hvorfor statussen blev sat, i sagens statushistorik.
            efter["status"] = e.byg_payload("sagsstatus", {
                "status": objekt.status,
                "kommentar": STATUSKOMMENTAR,
            })
        if efter:
            krop[EFTER_OPRETTELSE] = efter
        return krop

    def send(self, objekt: Sag, payload: dict[str, Any]) -> tuple[str, str | None]:
        efter = payload.get(EFTER_OPRETTELSE) or {}
        oprettelse = {k: v for k, v in payload.items() if k != EFTER_OPRETTELSE}

        # Før oprettelse: findes sagen allerede? Fanger det tilfælde hvor et
        # tidligere kald nåede frem, men svaret gik tabt.
        sbsys_id: str | None = self._find_identisk(objekt, oprettelse)
        sbsys_nummer: str | None = None
        if sbsys_id:
            log.warning("Sag %s fandtes allerede i SBSYS (id=%s) — genbruger",
                        kort_noegle(objekt.kilde_noegle), sbsys_id)
        else:
            op = self.k.endpoints.operation("opret_sag")
            svar = self.k.klient.kald(
                op.metode, op.sti, operation="opret_sag", json=oprettelse,
                forventede_status=op.forventede_status,
                kontekst={"objekt_type": "sag", "kilde_noegle": objekt.kilde_noegle},
            )
            krop = svar.krop or {}
            fundet_id = _foerste(krop, "Id", "id", "SagId", "sagId")
            if not fundet_id:
                raise PermanentFejl(
                    "SBSYS returnerede intet id ved oprettelse af sag",
                    korrelations_id=svar.korrelations_id,
                )
            sbsys_id = str(fundet_id)
            # SagDtoV10: Nummer er sagsnummeret som tekst. SagsNummer er et
            # OBJEKT (SagNummerDto) og maa ikke ende i konv.id_map som tekst.
            nummer = krop.get("Nummer") if isinstance(krop, dict) else None
            sbsys_nummer = nummer if isinstance(nummer, str) else None

        # Kommentar og status saettes OGSAA paa en genfundet sag: var den
        # oprettet, men svaret tabt, naaede de aldrig at blive sat.
        # Kommentaren foerst: en lukket status kan spaerre for redigering.
        if efter.get("kommentar"):
            self._saet_kommentar(objekt, sbsys_id, efter["kommentar"])
        if efter.get("status"):
            self._saet_status(objekt, sbsys_id, efter["status"])
        return sbsys_id, sbsys_nummer

    def _saet_kommentar(self, objekt: Sag, sbsys_id: str, kommentar: str) -> None:
        """GET /api/sag/{id}, saet Kommentar, PUT hele sagen tilbage.

        PUT'en kraever hele SagDtoV10 (prøvet i prod 26-11-2026). Fejler
        det, afvises sagen IKKE — samme grund som ved status: sagen findes,
        og en dublet er vaerre end en manglende kommentar. Advarsel i
        konv.afvist (regel 'kommentar_ikke_sat')."""
        e = self.k.endpoints
        kontekst = {"objekt_type": "sag", "kilde_noegle": objekt.kilde_noegle}
        try:
            hent = e.operation("hent_sag", sag_id=sbsys_id)
            sag = self.k.klient.kald(hent.metode, hent.sti, operation="hent_sag",
                                     forventede_status=hent.forventede_status,
                                     kontekst=kontekst).krop
            if not isinstance(sag, dict):
                raise PermanentFejl("hent_sag: svaret er ikke en sag")
            if _samme_tekst(sag.get("Kommentar"), kommentar):
                log.info("Sag %s (id=%s): kommentaren kom med ved oprettelsen - ingen PUT",
                         kort_noegle(objekt.kilde_noegle), sbsys_id)
                return
            log.info("Sag %s (id=%s): kommentaren kom IKKE med ved oprettelsen - saettes med PUT",
                     kort_noegle(objekt.kilde_noegle), sbsys_id)
            sag["Kommentar"] = kommentar
            put = e.operation("opdater_sag", sag_id=sbsys_id)
            self.k.klient.kald(put.metode, put.sti, operation="opdater_sag", json=sag,
                               forventede_status=put.forventede_status, kontekst=kontekst)
        except (VaertsnavnFejl, CertifikatFejl):
            raise
        except (PermanentFejl, ForbigaaendeFejl) as fejl:
            log.warning("Sag %s oprettet (id=%s), men kommentaren kunne ikke saettes: %s",
                        kort_noegle(objekt.kilde_noegle), sbsys_id, maskér(str(fejl)))
            self._afvis(
                objekt.kilde_noegle, "kommentar_ikke_sat", "advarsel",
                f"Sagen er oprettet i SBSYS (id={sbsys_id}), men kommentaren blev ikke "
                f"sat: {maskér(str(fejl))}",
                felt="kommentar", vaerdi=objekt.go_sagsnummer,
            )

    def _saet_status(self, objekt: Sag, sbsys_id: str, krop: dict[str, Any]) -> None:
        """PUT /api/sag/{id}/status. Fejler den, afvises sagen IKKE.

        Sagen findes paa dette tidspunkt i SBSYS. Rejste vi fejlen videre,
        ville sagen staa som 'failed' i konv.id_map uden sit SBSYS-id —
        og naeste koersel ville oprette den IGEN, medmindre genfindingen
        rammer. En forkert status kan rettes; en dublet af en boernesag er
        langt vaerre. Fejlen skrives derfor som en advarsel i konv.afvist
        (regel 'status_ikke_sat'), og sagen markeres som indlaest.

        Konfigurationsfejl (DNS, certifikat, 401) stopper stadig kørslen.
        """
        op = self.k.endpoints.operation("opdater_sagsstatus", sag_id=sbsys_id)
        try:
            self.k.klient.kald(
                op.metode, op.sti, operation="opdater_sagsstatus", json=krop,
                forventede_status=op.forventede_status,
                kontekst={"objekt_type": "sag", "kilde_noegle": objekt.kilde_noegle},
            )
        except (VaertsnavnFejl, CertifikatFejl):
            raise
        except (PermanentFejl, ForbigaaendeFejl) as e:
            log.warning("Sag %s oprettet (id=%s), men status kunne ikke saettes: %s",
                        kort_noegle(objekt.kilde_noegle), sbsys_id, maskér(str(e)))
            self._afvis(
                objekt.kilde_noegle, "status_ikke_sat", "advarsel",
                f"Sagen er oprettet i SBSYS (id={sbsys_id}), men status blev ikke "
                f"sat: {maskér(str(e))}",
                felt="status", vaerdi=str(objekt.status),
            )

    def _part_reference(self, objekt: Sag) -> dict[str, Any]:
        """Slå sagens primære part op i SBSYS og byg referencen.

        Raises:
            ValideringsFejl: Hvis sagen ingen part har, eller parten ikke
                findes i SBSYS. Begge dele afviser ÉN sag og stopper ikke
                bølgen — en borger der er udvandret eller afgået skal en
                fagperson tage stilling til.
        """
        if not objekt.primaer_part_noegle:
            raise ValideringsFejl(
                "Sagen har ingen primær part, og SBSYS' skabelon kræver en. "
                "Kun emnesager må oprettes uden part (27-11-2026).",
                regel="mangler_part", felt="primaer_part_noegle",
            )

        raekke = self.k.db.hent_en(
            "SELECT part_type, cpr, cvr, navn FROM stg.part WHERE kilde_noegle = ?",
            objekt.primaer_part_noegle,
        )
        if not raekke:
            raise ValideringsFejl(
                f"Parten {kort_noegle(objekt.primaer_part_noegle)} findes ikke i stg.part",
                regel="ukendt_part", felt="primaer_part_noegle",
            )

        er_firma = (raekke.get("part_type") or "").lower() in ("virksomhed", "firma")
        nummer = (raekke.get("cvr") if er_firma else raekke.get("cpr")) or ""
        nummer = nummer.strip()
        if not nummer:
            raise ValideringsFejl(
                "Parten har hverken CPR eller CVR, og SBSYS slår op på nummeret",
                regel="part_uden_nummer", felt="cpr",
            )

        part_id = self._slaa_part_op(nummer, er_firma)
        # Huskes til genfindingen: soegningen angiver parten med CPR/CVR,
        # ikke med PartId.
        self._part_nummer[objekt.kilde_noegle] = (_formatér_nummer(nummer, er_firma), er_firma)
        return {
            self.k.endpoints.felt("part", "part_id"): part_id,
            self.k.endpoints.felt("part", "part_type"): "Firma" if er_firma else "Person",
        }

    def _ansvarlig(self, raekke: dict[str, Any]) -> str | None:
        """Sagsbehandleren som SBSYS-bruger-id.

        PPR-skabelonerne er beskyttede (ErBeskyttet), og så KRÆVER SBSYS en
        behandler — ellers HTTP 400 "Sagen skal beskyttes, men skabelonen
        angiver ikke en behandler" (prod 20-11-2026). Rækkefølgen:

        1. ``TEST_SAGSBEHANDLER_AZ`` i .env tvinger én behandler på alle
           sager. Kun til test; der advares ved hver kørsel.
        2. En række i mapping/bruger.csv (GO-login -> SBSYS-id).
        3. GO's egen sagsbehandler, slået op i SBSYS på AZ-identen. Samme
           AD-identitet i begge systemer — ikke et gæt.

        4. Ellers MBU-systemkontoen (den konto konverteringen kører som) —
           besluttet 26-11-2026. Så afviser SBSYS aldrig en beskyttet sag
           for manglende behandler.
        """
        tvungen = os.environ.get(TVUNGEN_SAGSBEHANDLER, "").strip()
        if tvungen:
            if not self._tvungen_advaret:
                log.warning("%s er sat: ALLE sager får samme sagsbehandler. "
                            "Kun til test.", TVUNGEN_SAGSBEHANDLER)
                self._tvungen_advaret = True
            bruger_id = self._bruger_id(tvungen)
            if bruger_id is None:
                raise ValideringsFejl(
                    f"{TVUNGEN_SAGSBEHANDLER} findes ikke som bruger i SBSYS",
                    regel="sagsbehandler_ikke_i_sbsys", felt="ansvarlig",
                )
            return str(bruger_id)

        go = raekke.get("ansvarlig_go")
        mappet = self.k.kodelister.slaa_op("bruger", go, paakraevet=False)
        if mappet:
            return mappet
        az = find_az(go)
        if az:
            bruger_id = self._bruger_id(az)
            if bruger_id is not None:
                return str(bruger_id)
        # 4. Ingen SBSYS-bruger: MBU-systemkontoen (besluttet 26-11-2026).
        log.warning("Sag %s: GO's sagsbehandler kan ikke kobles til en SBSYS-bruger "
                    "- sagen får MBU-systemkontoen",
                    kort_noegle(str(raekke.get("kilde_noegle"))))
        return str(self._brugere.systemkonto())

    def _bruger_id(self, az: str) -> int | None:
        return self._brugere.bruger_id(az)

    def _slaa_part_op(self, nummer: str, er_firma: bool) -> int:
        """CPR/CVR -> SBSYS' interne PartId. Resultatet huskes for kørslen.

        Mange sager deler den samme borger — søskende, flere PPR-forløb —
        og uden cachen ville vi slå den samme op tusindvis af gange.
        """
        if nummer in self._part_cache:
            return self._part_cache[nummer]

        op = self.k.endpoints.operation("soeg_firma" if er_firma else "soeg_person")
        felt = self.k.endpoints.felt("part", "cvr" if er_firma else "cpr")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation=op.sti.strip("/").replace("/", "_"),
            json={felt: _formatér_nummer(nummer, er_firma)}, forventede_status=(200, 204, 404),
        )
        traef = self.k.endpoints.soegningstraef(svar.krop)
        if not traef:
            raise ValideringsFejl(
                f"{'CVR' if er_firma else 'CPR'} findes ikke i SBSYS. Historiske, "
                f"udenlandske og fiktive numre er netop det spørgsmål A8 handler om.",
                regel="part_ikke_i_sbsys", felt="cvr" if er_firma else "cpr",
            )
        part_id = _foerste(traef[0], "Id", "id", "PartId")
        if not part_id:
            raise ValideringsFejl("Partsøgningen returnerede intet id", regel="part_uden_id")
        self._part_cache[nummer] = int(part_id)
        return int(part_id)

    def _find_identisk(self, objekt: Sag, payload: dict[str, Any]) -> str | None:
        """Findes sagen allerede i SBSYS?

        GO-nøglen skrives ikke ind i SBSYS (besluttet 11-11-2026), så der
        er ingen fremmed nøgle at søge på. Tilbage står en **naturlig
        nøgle**: den primære part plus sagens titel. To sager på samme
        barn med samme titel er ikke til at skelne fra hinanden — og det
        er præcis derfor kun ÉT træf accepteres. Flere træf behandles som
        "ved ikke", og sagen fejler hellere end at hægte dokumenter og
        indsatser på den forkerte.

        Kriterierne følger SagSearchQueryV29: ``Titel`` og ``PrimaerPerson``
        (eller ``PrimaertFirma``) med CPR/CVR. Den første udgave sendte
        ``SagsTitel`` og et ``PrimaryPart``-objekt, som søgningen ikke kender.

        Hvert træf kontrolleres desuden HER: titlen skal være identisk, og
        sagens primære part skal være præcis samme PartId. Ignorerer SBSYS
        et kriterium, svarer den med vilkårlige sager — og uden kontrollen
        kunne en sag på et andet barn med samme titel blive genbrugt.

        Det er svagere end et nøglefelt. Konsekvensen står i ADR-0001:
        et tabt svar på en sag med en tvetydig titel skal ryddes op i
        hånden med ``konv genskab``.
        """
        e = self.k.endpoints
        try:
            op = e.operation("soeg_sag")
        except Exception:
            return None

        part = payload.get(e.felt("sag", "primaer_part"))
        nummer = self._part_nummer.get(objekt.kilde_noegle)
        if not part or not objekt.titel or not nummer:
            # Uden en part at afgrænse på ville søgningen ramme hele SBSYS.
            # Så hellere ingen kontrol end en der tilfældigvis rammer.
            return None
        vaerdi, er_firma = nummer
        part_id = part.get(e.felt("part", "part_id"))

        kriterier = e.byg_payload("sagssoegning", {
            "titel": objekt.titel,
            ("primaert_firma" if er_firma else "primaer_person"): vaerdi,
        })
        # SBSYS' søgning er en POST med kriterierne i kroppen, ikke en GET
        # med query-parametre (en GET giver 405), og paging-felterne SKAL
        # med. Parterne kommer kun med i resultatet, når der bedes om dem.
        som_krop = op.metode.upper() in ("POST", "PUT", "PATCH")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation="soeg_sag",
            json=e.soegningskrop(kriterier) if som_krop else None,
            params={"InkluderParterIResultat": "true"} if som_krop else kriterier,
            forventede_status=(200, 204, 404),
        )
        if svar.status != 200 or not svar.krop:
            return None
        traef = [
            r for r in e.soegningstraef(svar.krop)
            if _samme_sag(r, objekt.titel, part_id, e.felt("part", "part_id"))
        ]
        if len(traef) == 1:
            return str(_foerste(traef[0], "Id", "id", "SagId") or "") or None
        return None


# Noeglen i payloaden for det der saettes EFTER oprettelsen. Underscoren er
# med vilje: SBSYS har ingen felter der starter med en, saa den kan aldrig
# kollidere med et rigtigt felt — og send() fjerner den foer kaldet.
EFTER_OPRETTELSE = "_efter_oprettelse"

# Kommentaren paa statusskiftet efter oprettelsen. Ses i sagens statushistorik.
STATUSKOMMENTAR = "Status overført ved konvertering fra GetOrganized"


def _formatér_nummer(nummer: str, er_firma: bool) -> str:
    """SBSYS vil have CPR med bindestreg (DDMMYY-NNNN); CVR som det er."""
    if er_firma or "-" in nummer:
        return nummer
    return f"{nummer[:6]}-{nummer[6:]}"


def _samme_sag(raekke: Any, titel: str, part_id: Any, part_felt: str) -> bool:
    """Er søgeresultatet præcis denne sag: samme titel og samme primære part?"""
    if not isinstance(raekke, dict) or part_id is None:
        return False
    raekkens_titel = raekke.get("SagsTitel") or raekke.get("Titel") or ""
    primaer = raekke.get("PrimaryPart") or {}
    return (
        str(raekkens_titel).strip() == titel.strip()
        and isinstance(primaer, dict)
        and str(primaer.get(part_felt)) == str(part_id)
    )


def _foerste(krop: Any, *navne: str) -> Any:
    if not isinstance(krop, dict):
        return None
    for navn in navne:
        if krop.get(navn) is not None:
            return krop[navn]
    return None
