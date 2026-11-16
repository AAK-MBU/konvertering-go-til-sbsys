/*
  999_testdata.sql — syntetiske testdata, så løsningen kan prøves af
  uden at vente på en leverance fra Netcompany.

  Indeholder INGEN rigtige persondata. CPR-numrene er ugyldige med vilje.

  Kør KUN i test:
    sqlcmd -S <server> -d SBSYS_KONV -i sql/999_testdata.sql

  Rydder op efter sig selv først, så den kan køres igen og igen.
*/

SET NOCOUNT ON;

IF EXISTS (SELECT 1 FROM konv.run WHERE miljoe = 'prod')
BEGIN
    RAISERROR('Der findes prod-kørsler i denne database. Testdata indlæses ikke.', 16, 1);
    RETURN;
END

/* --- Ryd tidligere testdata ---------------------------------------- */
/* Hver tabel scriptet skriver i, skal ogsaa ryddes her — ellers fejler
   anden koersel paa en primaernoegle, og det er en irriterende maade at
   opdage det paa. Testen test_sql_arity.py haandhaever det.

   konv.id_map ryddes BEVIDST IKKE. Den er ikke testdata: den er
   fortegnelsen over hvad der faktisk findes i SBSYS. Sletter man en
   'loaded'-raekke, mens objektet stadig ligger i SBSYS, er forbindelsen
   mellem de to vaek — dokumenter kan ikke laenger placeres paa sagen, og
   naeste koersel opretter en DUBLET. Skal testobjekter koeres igen, saa
   brug `konv nulstil --ogsaa-indlaeste`, som siger hvad den goer. */
DELETE FROM konv.afvist   WHERE kilde_noegle LIKE 'TEST-%';
DELETE FROM stg.indsats   WHERE kilde_noegle LIKE 'TEST-%';
DELETE FROM stg.notat     WHERE kilde_noegle LIKE 'TEST-%';
DELETE FROM stg.dokument  WHERE kilde_noegle LIKE 'TEST-%';
DELETE FROM stg.sag       WHERE kilde_noegle LIKE 'TEST-%';
DELETE FROM stg.part      WHERE kilde_noegle LIKE 'TEST-%';

/* Sig til, hvis der stadig staar testobjekter som indlaest i SBSYS.
   Saa er kildedataene nu nye, mens id_map peger paa de gamle objekter. */
IF EXISTS (SELECT 1 FROM konv.id_map WHERE kilde_noegle LIKE 'TEST-%' AND status = 'loaded')
    PRINT 'BEMAERK: der staar testobjekter som indlaest i konv.id_map. '
        + 'De findes stadig i SBSYS og genbruges. Vil du oprette dem paa ny, '
        + 'saa koer: konv nulstil --boelge ppr --ogsaa-indlaeste';

/* --- Mapninger, så de påkrævede opslag kan lykkes ------------------- */
MERGE map.kodeliste AS t
USING (VALUES
    ('kle',      N'17.01.05', N'17.01.05', 'map'),
    ('kle',      N'27.24.03', N'27.24.03', 'map'),
    ('sagstype', N'PPR-sag',  N'1',        'map'),
    /* Sagsskabelonen er blokerende: uden den afvises hver eneste sag med
       manglende_mapping, foer noget andet bliver afproevet. Id'et er
       opdigtet — attrappen validerer det ikke. Mod et rigtigt SBSYS skal
       det vaere et id fra `konv opslag skabeloner`. */
    ('sagsskabelon', N'PPR-sag', N'332',   'map'),
    ('afdeling', N'PPR Nord', N'4711',     'map'),
    ('bruger',   N'ab1234',   N'9000',     'map'),
    /* Bevidst uden sbsys_vaerdi: skal udløse en afvisning */
    ('kle',      N'GO-LOKAL-042', NULL,    'map'),
    /* Indsatser: GO's Ydelse og Bevillingstype -> SBSYS' tripel */
    ('paragraf',        N'SEL 11.3',         N'1', 'map'),
    ('foranstaltning',  N'Psykologsamtaler', N'1', 'map'),
    ('tilbud',          N'Psykologsamtaler', N'1', 'map')
) AS s (liste, go_vaerdi, sbsys_vaerdi, handling)
ON t.liste = s.liste AND t.go_vaerdi = s.go_vaerdi
WHEN NOT MATCHED THEN
    INSERT (liste, go_vaerdi, sbsys_vaerdi, handling)
    VALUES (s.liste, s.go_vaerdi, s.sbsys_vaerdi, s.handling);

/* --- Fem sager i bølgen 'ppr' --------------------------------------- */
/* --- Parter ---------------------------------------------------------
   SBSYS' sagsskabelon kraever en part, og den slaas op paa CPR/CVR.
   Standardnumrene er fiktive; findes de ikke i testmiljoeets CPR-opslag,
   afvises sagen med regel 'part_ikke_i_sbsys' — hvilket er praecis den
   adfaerd spoergsmaal A8 handler om.

   Skal resten af kaeden testes (skabelon, dokument, notat, indsats), skal
   der bruges et CPR der FINDES i SBSYS' testmiljoe. Sat udefra, saa det aldrig havner
   i filen:

     uv run konv db koer sql/999_testdata.sql --var cpr1=XXXXXXXXXX

   Et rigtigt CPR i cpr1 og det fiktive i cpr2 giver begge udfald i samme
   koersel: fire sager der kan oprettes, én der afvises paa parten.      */
INSERT INTO stg.part (kilde_noegle, part_type, cpr, navn)
VALUES
    ('TEST-PART-001', 'person', '$(cpr1|0101011234)', N'Test Testesen'),
    ('TEST-PART-002', 'person', '$(cpr2|0202022345)', N'Anden Testesen');
GO

INSERT INTO stg.sag
    (kilde_noegle, boelge, go_sagsnummer, titel, kle_go, sagstype_go,
     afdeling_go, ansvarlig_go, status_go, oprettet_dato, primaer_part_noegle)
VALUES
    ('TEST-SAG-001', 'ppr', 'PPR-2023-000001-001', N'Testsag et',   '17.01.05', 'PPR-sag', 'PPR Nord', 'ab1234', 'Active', '2023-01-15', 'TEST-PART-001'),
    ('TEST-SAG-002', 'ppr', 'PPR-2023-000002-001', N'Testsag to',   '27.24.03', 'PPR-sag', 'PPR Nord', 'ab1234', 'Active', '2023-04-02', 'TEST-PART-001'),
    ('TEST-SAG-003', 'ppr', 'PPR-2024-000003-001', N'Testsag tre',  '17.01.05', 'PPR-sag', 'PPR Nord', 'ab1234', 'Closed', '2024-02-20', 'TEST-PART-002'),
    /* Mangler KLE-mapping -> skal havne i konv.afvist, ikke i SBSYS */
    ('TEST-SAG-004', 'ppr', 'PPR-2024-000004-001', N'Testsag fire', 'GO-LOKAL-042', 'PPR-sag', 'PPR Nord', 'ab1234', 'Active', '2024-06-01', 'TEST-PART-001'),
    /* Ukendt afdeling -> også blokerende */
    ('TEST-SAG-005', 'ppr', 'PPR-2024-000005-001', N'Testsag fem',  '17.01.05', 'PPR-sag', N'Ukendt Enhed', 'ab1234', 'Active', '2024-09-10', NULL);

/* --- Dokumenter på de to første sager -------------------------------
   fil_relativ_sti peger på filer du selv opretter — se docs/test-selv.md.
   fil_status sættes til NULL, så `konv filer scan` har noget at lave.      */
INSERT INTO stg.dokument
    (kilde_noegle, sag_noegle, boelge, titel, kategori_go, dokument_dato,
     retning, fil_relativ_sti, fil_navn)
VALUES
    ('TEST-DOK-001', 'TEST-SAG-001', 'ppr', N'Indstilling',      N'Notat', '2023-01-16', 'internt',    'PPR-2023-000001-001/16391281.txt', '16391281.txt'),
    ('TEST-DOK-002', 'TEST-SAG-001', 'ppr', N'Statusbeskrivelse', N'Notat', '2023-03-01', 'udgaaende',  'PPR-2023-000001-001/16391282.txt', '16391282.txt'),
    ('TEST-DOK-003', 'TEST-SAG-002', 'ppr', N'Referat',          N'Notat', '2023-04-05', 'internt',    'PPR-2023-000002-001/16391283.txt', '16391283.txt'),
    /* Filen findes ikke -> skal give fil_status = 'mangler' */
    ('TEST-DOK-004', 'TEST-SAG-002', 'ppr', N'Bortkommet bilag', N'Notat', '2023-05-05', 'indgaaende', 'PPR-2023-000002-001/99999999.txt', '99999999.txt');

PRINT 'Testdata indlæst:';
PRINT '  5 sager i boelgen ppr — to af dem skal afvises (manglende mapping)';
PRINT '  4 dokumenter — et af dem har ingen fil';
PRINT '';
PRINT 'Naeste skridt: se docs/test-selv.md';

/* --- Journalnotater -------------------------------------------------
   Tegnet '<' er med med vilje: SBSYS renderer notefeltet som HTML, og
   uescapet bliver det til begyndelsen paa et tag. Notatet skal komme
   over som den tekst der stod i GO.                                    */
INSERT INTO stg.notat (kilde_noegle, sag_noegle, boelge, titel, tekst, forfatter_go, dato)
VALUES
    ('TEST-NOTAT-001', 'TEST-SAG-001', 'ppr', N'Telefonsamtale',
     N'Mor ringede. Bor <hos> mormor i en periode.' + CHAR(13) + CHAR(10) + N'Aftalt nyt moede.',
     'ab1234', '2023-02-01'),
    ('TEST-NOTAT-002', 'TEST-SAG-001', 'ppr', N'Observation i klassen',
     N'Fulgte undervisningen i 45 minutter.', 'ab1234', '2023-03-12');
GO

/* --- Indsatser ------------------------------------------------------
   Den sidste har en ukendt ydelse og skal afvises med manglende_mapping,
   saa reparationssloejfen kan proeves. Oekonomifelterne er med vilje
   ikke repraesenteret: oekonomi er ikke en del af konverteringen.      */
INSERT INTO stg.indsats
    (kilde_noegle, sag_noegle, boelge, ydelse_go, bevillingstype_go,
     periode_start, periode_slut, antal_enheder, enhedstype_go)
VALUES
    ('TEST-INDSATS-001', 'TEST-SAG-001', 'ppr', N'Psykologsamtaler', N'SEL 11.3',
     '2023-02-01', '2023-12-31', 12, N'Time'),
    /* Aaben indsats: ingen slutdato */
    ('TEST-INDSATS-002', 'TEST-SAG-001', 'ppr', N'Psykologsamtaler', N'SEL 11.3',
     '2024-01-15', NULL, NULL, NULL),
    /* Ukendt ydelse -> skal afvises, ikke sendes */
    ('TEST-INDSATS-003', 'TEST-SAG-001', 'ppr', N'GO-UKENDT-YDELSE', N'SEL 11.3',
     '2024-03-01', NULL, NULL, NULL);
GO
