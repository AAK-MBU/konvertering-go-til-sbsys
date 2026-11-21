/*
  998_gofake_kilde.sql — en FALSK GOStaging til simulering af en rigtig
  PPR-konvertering.

  Tabellerne i skemaet gofake faar PRAECIS GOStaging's form (kolonner og
  typer kopieres med SELECT TOP (0) * INTO), men indholdet er opdigtet.
  Saa koeres den RIGTIGE extract mod dem:

      uv run konv db koer sql\998_gofake_kilde.sql --var cpr1=<CPR der findes i testmiljoeet>
      uv run konv db koer sql\100_extract_skabelon.sql --kilde SBSYS_KONV.gofake
      uv run konv mapping load

  `mapping load` EFTER extract: extract laegger nye GO-vaerdier i kodelisten
  med tom SBSYS-vaerdi, og mapping load udfylder dem. Med KONV_MILJOE=test
  laegges testmiljoeets id'er og feltnavne fra mapping\testmiljoe\ ovenpaa.

  Hele kaeden — extract, noegler, mapning, loaders — koerer som med det
  rigtige dump, men der kommer ingen rigtige data i naerheden af SBSYS'
  testmiljoe.

  Modellen (bekraeftet 18-11-2026): PPR-mellemlaget er vaek. Hver
  foranstaltningsmappe er en raekke i Cases med typen i Sagsprofil og
  bliver sin egen sag i SBSYS. Hver raekke i Foranstaltningsdata er én
  indsats paa den sag (Foranstaltningsdata.SagsId = Cases.GOSagsID).

  Ét barn (cpr1) med to foranstaltningsmapper:

    PPR-2023-900001-001  ABA       — 6 indsatser
    PPR-2023-900002-001  Psykolog  — 3 indsatser

  Kun de kolonner hver type BRUGER i GO er udfyldt (konverteringsarket +
  profileringen i sql/104). Vaerdierne er taget fra arkets vaerdilister, og
  formaterne er dumpets: datoer dd-mm-aaaa, Timer t:mm, sagsbehandler som
  AZ-ident. Med vilje er de besvaerlige tilfaelde fra dumpet med:
    * tom Fra-dato             -> 1900-01-01
    * tom Timer og Sagsbehandler
    * Timer '1:75'             (kan kun vaere decimaltal)
    * en "Nej"-vaerdi           (xxxTilflytter -> Indstilling)
    * to HELT ens raekker      (aegte dublet)

  AZ-identen az99999 og alle navne er opdigtede. Institutionernes navne er
  fra arkets vaerdilister og er ikke persondata.

  Koeres igen og igen: tabellerne genskabes hver gang.
*/

SET NOCOUNT ON;

IF EXISTS (SELECT 1 FROM konv.run WHERE miljoe = 'prod')
BEGIN
    RAISERROR('Der findes prod-koersler i denne database. Falske kildedata indlaeses ikke.', 16, 1);
    /* RETURN stopper kun denne batch. NOEXEC stopper ogsaa de efterfoelgende,
       ogsaa naar filen koeres i SSMS. */
    SET NOEXEC ON;
END
GO

IF SCHEMA_ID('gofake') IS NULL
    EXEC ('CREATE SCHEMA gofake');
GO

/* --- Tabellerne: GOStaging's form, ingen raekker ----------------------
   Alle syv som extract laeser fra. De tomme (dokumenter, journalark,
   workflows, kontakter) skal stadig findes, ellers fejler extract. */
DROP TABLE IF EXISTS gofake.Cases;
DROP TABLE IF EXISTS gofake.Contacts;
DROP TABLE IF EXISTS gofake.Documents;
DROP TABLE IF EXISTS gofake.DocumentSHA;
DROP TABLE IF EXISTS gofake.Foranstaltningsdata;
DROP TABLE IF EXISTS gofake.GOWorkflows;
DROP TABLE IF EXISTS gofake.JournalNotes;

SELECT TOP (0) * INTO gofake.Cases               FROM GOStaging.dbo.Cases;
SELECT TOP (0) * INTO gofake.Contacts            FROM GOStaging.dbo.Contacts;
SELECT TOP (0) * INTO gofake.Documents           FROM GOStaging.dbo.Documents;
SELECT TOP (0) * INTO gofake.DocumentSHA         FROM GOStaging.dbo.DocumentSHA;
SELECT TOP (0) * INTO gofake.Foranstaltningsdata FROM GOStaging.dbo.Foranstaltningsdata;
SELECT TOP (0) * INTO gofake.GOWorkflows         FROM GOStaging.dbo.GOWorkflows;
SELECT TOP (0) * INTO gofake.JournalNotes        FROM GOStaging.dbo.JournalNotes;
GO

/* --- Sagerne: én pr. foranstaltningsmappe -----------------------------
   StartDato/LukketDato skrives aaaa-mm-dd: det tolkes ens, uanset om
   kolonnen er tekst eller dato i dumpet. */
INSERT INTO gofake.Cases
    ([Organisering], [Sagsnavn], [CPR], [PartNavn], [GOSagsID], [Sagsstatus],
     [Sagsprofil], [Sagsbehandler], [Afdeling], [KLE], [StartDato], [LukketDato])
VALUES
    (N'PPR', N'ABA', N'$(cpr1|0101011234)', N'Test Testesen',
     N'PPR-2023-900001-001', N'Åben', N'ABA', N'az99999', N'PPR',
     N'17.01.05', N'2023-01-10', NULL),
    (N'PPR', N'Psykolog', N'$(cpr1|0101011234)', N'Test Testesen',
     N'PPR-2023-900002-001', N'Lukket', N'Psykolog', N'az99999', N'PPR',
     N'17.01.05', N'2022-02-01', N'2022-07-01');
GO

/* --- ABA: Visiteret, Fra, Til, Sagsbehandler, ABAGruppe, Ydelse,
       SkoleEllerInstitution, Betalingssted, Timer ----------------------- */
INSERT INTO gofake.Foranstaltningsdata
    ([SagsId], [Indholdstype], [Visiteret], [Fra], [Til], [Sagsbehandler],
     [ABAGruppe], [Ydelse], [SkoleEllerInstitution], [Betalingssted], [Timer])
VALUES
    /* Almindelig, afsluttet */
    (N'PPR-2023-900001-001', N'ABA', N'05-01-2023', N'15-01-2023', N'31-12-2023', N'az99999',
     N'Fase 1 (3 år)', N'ABA-supervisor', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', N'7:30'),
    /* Aaben: ingen Til */
    (N'PPR-2023-900001-001', N'ABA', N'20-12-2023', N'01-01-2024', NULL, N'az99999',
     N'Fase 2 (3 år)', N'ABA-træner – pædagogløn', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', N'3:00'),
    /* Tom Fra, tom Timer, tom Sagsbehandler */
    (N'PPR-2023-900001-001', N'ABA', N'01-03-2023', NULL, NULL, NULL,
     N'Fase 1 (3 år)', N'Øvrige personale – pædagog', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', NULL),
    /* Timer der kun kan vaere et decimaltal */
    (N'PPR-2023-900001-001', N'ABA', N'05-01-2023', N'01-02-2023', N'30-06-2023', N'az99999',
     N'Fase 1 (3 år)', N'Øvrige personale – lærer', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', N'1:75'),
    /* Aegte dublet: to HELT ens raekker */
    (N'PPR-2023-900001-001', N'ABA', N'05-01-2023', N'01-07-2023', N'31-12-2023', N'az99999',
     N'Fase 1 (3 år)', N'ABA-supervisor', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', N'2:00'),
    (N'PPR-2023-900001-001', N'ABA', N'05-01-2023', N'01-07-2023', N'31-12-2023', N'az99999',
     N'Fase 1 (3 år)', N'ABA-supervisor', N'225 Børnehuset Vores Sted', N'158 Møllevang dagtilbud', N'2:00');

/* --- Psykolog: Visiteret, Fra, Til, Sagsbehandler, Indstillingsaarsag -- */
INSERT INTO gofake.Foranstaltningsdata
    ([SagsId], [Indholdstype], [Visiteret], [Fra], [Til], [Sagsbehandler], [Indstillingsaarsag])
VALUES
    /* Almindelig, afsluttet */
    (N'PPR-2023-900002-001', N'Psykolog', N'01-02-2022', N'10-02-2022', N'30-06-2022', N'az99999',
     N'Indstillingsskema'),
    /* En "Nej"-vaerdi fra arket: xxxTilflytter -> Indstilling */
    (N'PPR-2023-900002-001', N'Psykolog', N'15-03-2022', N'01-04-2022', N'30-06-2022', N'az99999',
     N'xxxTilflytter'),
    /* Uden sagsbehandler og indstillingsaarsag, aaben */
    (N'PPR-2023-900002-001', N'Psykolog', N'01-05-2022', N'05-05-2022', NULL, NULL,
     NULL);
GO

PRINT 'Falsk GOStaging klar i SBSYS_KONV.gofake:';
PRINT '  2 sager (ABA, Psykolog) paa samme barn, 9 foranstaltninger.';
PRINT 'Naeste: uv run konv db koer sql\100_extract_skabelon.sql --kilde SBSYS_KONV.gofake';
