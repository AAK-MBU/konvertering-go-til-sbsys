"""Fejltyper.

Skelnen mellem "prøv igen" og "prøv aldrig igen" er hele forskellen på en
konvertering der selv arbejder sig igennem natten, og en der står stille
til nogen kigger på den.
"""

from __future__ import annotations


class KonverteringsFejl(Exception):
    """Basis for alle fejl i løsningen."""


class SbsysFejl(KonverteringsFejl):
    def __init__(
        self,
        besked: str,
        *,
        http_status: int | None = None,
        fejl_kode: str | None = None,
        korrelations_id: str | None = None,
        krop: str | None = None,
    ) -> None:
        super().__init__(besked)
        self.http_status = http_status
        self.fejl_kode = fejl_kode
        self.korrelations_id = korrelations_id
        self.krop = krop


class ForbigaaendeFejl(SbsysFejl):
    """Netværk, 429, 5xx, timeout. Genforsøges med backoff."""


class PermanentFejl(SbsysFejl):
    """4xx bortset fra 429. Data er forkerte — genforsøg hjælper ikke.

    Objektet sættes til 'failed' og lander i afvisningsrapporten,
    så en fagperson kan tage stilling.
    """


class VaertsnavnFejl(PermanentFejl):
    """Værtsnavnet kunne ikke slås op i DNS.

    Arver fra PermanentFejl, fordi det er en konfigurationsfejl og ikke et
    netværksglimt: genforsøg giver samme svar, bare senere. Med fem forsøg og
    voksende ventetid koster den slags et halvt minut pr. objekt.
    """


class CertifikatFejl(PermanentFejl):
    """Serverens TLS-certifikat kunne ikke verificeres.

    Som VaertsnavnFejl: en konfigurationsfejl, ikke et netværksglimt.
    Genforsøg giver nøjagtig samme svar, bare senere.
    """


def _i_fejlkaeden(fejl: BaseException, tegn: tuple[str, ...]) -> bool:
    """Led efter kendetegn i hele årsagskæden.

    httpx pakker den underliggende ssl/socket-fejl ind, så teksten ligger
    typisk to-tre niveauer nede i __cause__/__context__.
    """
    aktuel: BaseException | None = fejl
    while aktuel is not None:
        besked = str(aktuel).lower()
        if any(t in besked for t in tegn):
            return True
        aktuel = aktuel.__cause__ or aktuel.__context__
    return False


def er_certifikatfejl(fejl: BaseException) -> bool:
    """Er fejlen en mislykket verifikation af serverens certifikat?"""
    return _i_fejlkaeden(fejl, (
        "certificate_verify_failed",
        "certificate verify failed",
        "self-signed certificate",
        "self signed certificate",
        "unable to get local issuer certificate",
        "certificate has expired",
        "hostname mismatch",
        "certificate is not valid for",
    ))


def raad_om_certifikat(vaert: str) -> str:
    """Den besked man gerne vil have haft første gang.

    En TLS-fejl mod et internt testmiljø med egen CA er ikke et nedbrud, men
    en manglende rodnøgle. Rækkefølgen her er bevidst: den rigtige løsning
    først, nødudgangen sidst — og med dens pris nævnt.
    """
    return (
        f"Certifikatet på {vaert} kunne ikke verificeres. Udvejene, i den "
        f"rækkefølge:\n"
        f"  1. Brug maskinens eget certifikatlager: SBSYS_VERIFY=system\n"
        f"     (på en kommunal pc ligger kommunens rod-CA der i forvejen)\n"
        f"  2. Peg på kommunens rod-CA: SBSYS_VERIFY=C:/sti/til/ca.pem\n"
        f"     (eksportér kæden fra browseren som Base-64 .cer og saml dem i "
        f"én .pem-fil)\n"
        f"  3. Få et certifikat på serveren der kan verificeres\n"
        f"  4. Kun i test: SBSYS_VERIFY=0 slår verifikationen fra. Så kan "
        f"forbindelsen aflyttes og ændres undervejs, og persondata om børn og "
        f"unge passerer den. Afvises i produktion."
    )


def er_navneopslagsfejl(fejl: BaseException) -> bool:
    """Er fejlen et mislykket DNS-opslag?

    Beskederne er platformsafhængige: Windows siger "getaddrinfo failed"
    (WSAHOST_NOT_FOUND, errno 11001), Linux "Name or service not known",
    macOS "nodename nor servname provided".
    """
    return _i_fejlkaeden(fejl, (
        "getaddrinfo failed",
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "11001",
    ))


class AutentifikationsFejl(SbsysFejl):
    """401/403. Stopper hele kørslen — det giver ingen mening at brænde
    5.000 objekter af på en udløbet servicekonto."""


class ValideringsFejl(KonverteringsFejl):
    """Data opfylder ikke den kanoniske model, eller mangler mapping.
    Objektet når aldrig frem til SBSYS."""

    def __init__(self, besked: str, *, regel: str = "validering", felt: str | None = None,
                 vaerdi: str | None = None) -> None:
        super().__init__(besked)
        self.regel = regel
        self.felt = felt
        self.vaerdi = vaerdi


class ManglerMapping(ValideringsFejl):
    def __init__(self, liste: str, vaerdi: str) -> None:
        super().__init__(
            f"Ingen mapping for {liste}='{vaerdi}'",
            regel="manglende_mapping",
            felt=liste,
            vaerdi=vaerdi,
        )
        self.liste = liste


class UdeladtMedVilje(KonverteringsFejl):
    """Objektet skal bevidst ikke konverteres (en mapning med handling =
    ignorer). Ikke en fejl: det markeres 'skipped' og tælles som sprunget
    over — ikke afvist, for der er intet at rette."""


class FilFejl(KonverteringsFejl):
    def __init__(self, besked: str, *, regel: str = "fil_fejl", sti: str | None = None) -> None:
        super().__init__(besked)
        self.regel = regel
        self.sti = sti
