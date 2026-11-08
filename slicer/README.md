# STL-slicing i Dokploy

Understøttet: én STL-model med ét filament eller ét Bambu Studio 3MF-projekt med flere farver og én plade på **Bambu Lab P2S med
0,4 mm dyse og Textured PEI Plate**. STL tolkes i millimeter. Modellen centreres
og arrangeres på pladen; orienteringen mod byggepladen (Z) ændres ikke automatisk,
men slicerens arrangement kan rotere i pladens plan. Automatisk støtte er slået fra.
Laghøjder: 0,12 / 0,20 / 0,28 mm. Infill: 10 / 15 / 20 / 40 / 100 %.

## To applikationer fra samme repo

Den eksisterende PrintPlatform-applikation bruger fortsat **Dockerfile** i repo-roden,
port 3000 og det eksisterende `/data`-volume. Bevar alle eksisterende miljøvariabler
og tilføj en ny tilfældig hemmelighed på mindst 32 tegn:

```dotenv
SLICER_TOKEN=YOUR_NEW_RANDOM_SLICER_TOKEN
```

Tokenet skal være forskelligt fra agentnøglen og webadgangskoden. Redeploy webappen.
Ingen ændringer eller installation på Pi'en er nødvendige for STL-funktionen.

Opret derefter en **ny Application** i samme Dokploy-projekt, fx `printplatform-slicer`:

- GitHub: `caspermartinsen/PrintPlatform`, branch `main`.
- Build type: **Dockerfile**.
- Dockerfile path: **`slicer/Dockerfile`**.
- Docker context path: **`.`** (repo-roden).
- Ingen offentlig port, intet domæne og intet permanent volume.
- Understøttet server: **Linux amd64**, mindst 2 CPU / 4 GiB RAM og ca. 3 GiB fri disk.
- Begræns containeren til én replica, ca. 2 CPU, 4 GiB RAM og højst 128 processer.
- Runtime-variabler:

```dotenv
PLATFORM_URL=https://print.martinsen.family
SLICER_TOKEN=THE_SAME_SLICER_TOKEN_AS_THE_WEBAPP
```

Workerens eneste eksterne forbindelse er udgående HTTPS til webappen. Den har
ingen printerkode eller Pi-agentnøgle, deler ikke datalager og kan ikke starte print.
Brug ikke Docker-socket eller privileged-mode. Adgang til privat internt HTTP er
valgfri via `SLICER_ALLOW_INTERNAL_HTTP=1`; brug kun dette på et privat Docker-netværk.

## Brug

1. Upload en `.stl` (maks. 50 MiB / 1.000.000 trekanter).
2. Drej og zoom 3D-modellen. Kontrollér dimensioner og orientering.
3. Vælg en fyldt AMS-plads med PLA Basic, PLA Matte eller PLA Translucent.
4. Vælg laghøjde og infill, og tryk **Slice model**.
5. Når slicing er klar, vises printbaner med lagvælger, anslået tid og gram.
6. Download eventuelt `.gcode.3mf`, eller vælg **Vælg filament og godkend** i køen.
7. Bekræft det slicede preview. Print kræver fortsat den eksisterende særskilte
   godkendelse, herunder tom byggeplade og `START PRINT` ved fysisk start.

**Upload og slicing godkender eller starter aldrig et print.** 3MF-projekter får et AMS-valg for hvert projektfilament. Den valgte AMS-plads
bestemmer slicerprofilen. Ved senere godkendelse kontrolleres AMS-indholdet igen.
Modellen kan slices igen, indtil jobbet godkendes. Godkendte/afsendte job kan ikke
om-slices. Et nyt slice-forsøg får et nyt id; et gammelt resultat kan ikke erstatte
et nyere. Et slice-forsøg reserveres i højst syv minutter. Ved afbrudt worker bliver
jobbet derefter *Slicing fejlede* ved næste statuskald eller worker-poll.
Klargør modellen igen; et udløbet resultat accepteres ikke. Under et aktivt
forsøg kan **Genstart slice-forsøg** fortsat bruges efter kontrol af workerens log. En ældre kørsel kan
fortsætte indtil timeout, men resultatet ignoreres, og kun det nye forsøg accepteres.

## Teknik og begrænsninger

Sliceren bruger officiel **Bambu Studio 02.08.04.57**, downloadet fra Bambu Labs
GitHub-release og verificeret med SHA-256. Docker bygger fuldt opløste profiler
med inheritance og G-code-includes fra samme release. `machine_full` til P2S
tilføjes fra de medfølgende modeldata, så output har model-id **N7**. Filamentfarve,
laghøjde og infill er de eneste brugerbestemte profiloverskrivninger. Ingen
uploadede scripts, shell-kommandoer, G-code-skabeloner eller eksterne profil-URL'er
accepteres. En slicerproces har 5 minutters timeout og køres som ikke-root.

Output valideres igen på serveren: én plade, alle anvendte filamenter, P2S/N7,
0,4 mm dyse, Textured PEI og den forventede filamentprofil/farve. Resultatet bliver
kun et køjob, ikke et godkendt print. Ugyldige resultater bliver *Slicing fejlede*.

Preview viser ekstruderende G0/G1-segmenter efter første lagmarkør, ikke
opvarmning, purge eller rejsebevægelser. Nye profiler har arc fitting slået fra.
Uventede buer og segmenter over loftet på 200.000 udelades med en tydelig besked.
Preview er en kontrolvisning, ikke en kollisionssimulering eller garanti for et
vellykket print. Tid og gram er slicerens estimater. WebGL kræves til 3D-visningen.
Den genererede `.gcode.3mf` kan stadig downloades til kontrol i Bambu Studio.

Kun én worker understøttes i første version. Bevar webappens `/data`-volume;
workerens midlertidige filer slettes efter hver opgave. Udløbne slicing-job markeres fejlede og
genforsøges manuelt. Gamle STL'er/resultater ryddes manuelt; samlet datakvote er
ca. 2 GiB / 1000 job. Slicing og preview til eksisterende `.gcode.3mf`-uploads
er ikke en del af denne tilføjelse; de følger fortsat det eksisterende forløb.

## Lokal test

```sh
python3 -m unittest cloud.test_slicing cloud.test_platform -v
cd frontend && npm ci && npm run build && node --test app.test.cjs
docker build -t printplatform-web .
docker build --platform linux/amd64 -f slicer/Dockerfile -t printplatform-slicer .
```

Tests bruger midlertidige lokale servere og sender ingen printerkommandoer.
Sliceren kan testes med `docker run --network none ...`, da selve slicing ikke
kræver netværk. Udviklingsverifikation skal udføres i simuleringstilstand.

Bambu Studio-kilde og licens (AGPL-3.0):
https://github.com/bambulab/BambuStudio/tree/v02.08.04.57
CLI-dokumentation: https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
3D-preview bruger Three.js (MIT): https://github.com/mrdoob/three.js/tree/r180


## 3MF-projekter og flere farver

Slicer-version 2 modtager native Bambu Studio-projekter. Farvede dele, assembly-transformer og malingsdata bevares. CLI får en fuld, betroet filamentprofil pr. projektfilament; maskine og proces sættes fortsat til P2S, 0,4 mm og Textured PEI. Klargøring understøtter de samme tre PLA-profiler som tidligere. Et AMS-valg for en anden materialetype afvises.

Importerede proces-/maskinindstillinger erstattes af de faste profiler. Egne per-lag-G-code, scripts og variable lagprofiler bruges ikke. Støtte er fortsat slået fra; prime tower aktiveres ved flere projektfilamenter. Projektets pladeplacering bevares, mens STL arrangeres som før. Kun én plade og millimeter understøttes. Alle faktiske outputfilamenters indeks, profil og farve kontrolleres mod valgene.

Cloud kræver en opdateret worker, før 3MF-projekter kan klargøres. Gamle workers får ikke sådanne jobs. Opdatér derfor begge Dokploy-applikationer. Pi-agent og lokal service skal desuden have plan-version 3, før flerfarvejobs kan godkendes eller afleveres.

CI bygger den fastlåste Bambu Studio-container og slicer et lille tofarvet 3MF-projekt med --network none. Testen kontrollerer både de to outputfilamenter og farveindeks i ekstruderingsbanerne. Den har ingen adgang til printeren.
