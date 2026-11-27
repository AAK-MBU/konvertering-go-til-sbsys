/* 920_seneste_afvisninger.sql — hvorfor blev noget afvist i SENESTE kørsel?

       uv run konv db koer sql\920_seneste_afvisninger.sql

   Kun læsning. Uden vaerdi og kilde_noegle, så outputtet kan deles:
   beskrivelsen er maskeret for CPR allerede ved indsættelsen, og
   regel/felt siger hvad der skal rettes. */

SET NOCOUNT ON;

DECLARE @run BIGINT = (SELECT MAX(run_id) FROM konv.afvist);

SELECT a.run_id,
       a.objekt_type,
       a.regel,
       a.alvorlighed,
       a.felt,
       a.beskrivelse
FROM   konv.afvist a
WHERE  a.run_id = @run
ORDER  BY a.afvist_id;
