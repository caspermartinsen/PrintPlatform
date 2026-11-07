# Printplatform til Raspberry Pi 3 / Debian 13

Projektet samler `service/`, `frontend/` og `deploy/`. Python 3.11+ og
standardbiblioteket er nok. Pi'ens eksisterende `open-bamboo-persano` er en separat
driverafhængighed; biblioteket genbygges eller opdateres ikke af installationen.

## Sikker adfærd

Standard er **simulate**: ingen driverproces, upload til printer eller fysisk
printstart. Upload opretter kun et køjob. Hvert job skal startes eksplicit.
Køen starter aldrig næste job automatisk. SQLite gemmer tilstand før driverkaldet.
En proceslås forhindrer flere serviceinstanser på samme datamappe.

`starting` reserverer printeren; `submitted` betyder kun, at driveren returnerede
0, ikke at printet er færdigt. Fejl, timeout eller afbrudt start bliver `unknown`.
Disse job genforsøges aldrig. Kontrollér printeren fysisk og brug `resolve`, når
den er færdig og tom. Dermed frigives reservationen. `resolve` stopper ikke print.
Printertelemetri kan aktiveres separat fra printstart (se nedenfor).
Brug aldrig status som bevis på en tom plade.

Upload er en rå HTTP-body, ikke multipart. Maks. 100 MiB pr. fil, 2 GiB samlet
lager og 1000 job. ZIP-medlemmer kontrolleres for traversal, dubletter, kryptering,
symlinks, CRC og størrelse; kun enkeltplade med `Metadata/plate_1.gcode` accepteres.
Valideringen beviser ikke korrekte temperaturer, dyse, filament eller printerprofil.
Brug kun betroede filer slicet til din P2S. API'et modtager ikke rå STL og slicer ikke.
Bevar gamle job til fejlsøgning; arkivering/sletning er en manuel driftsopgave.

## Installation på Pi

Kopiér projektmappen til Pi'en. Fra mappen:

```sh
sudo sh deploy/install.sh
sudo systemd-analyze verify /etc/systemd/system/print-service.service
sudo systemctl enable --now print-service
sudo systemctl status print-service
```

Installationen genererer en tilfældig API-nøgle i
`/etc/print-service/environment` (root, 0600). Systemd indlæser miljøet.
Installation starter ikke automatisk servicen og overskriver ikke eksisterende
hemmeligheder. Efter ændringer: `sudo systemctl restart print-service`.

Kør først i simulate. Forbered driverintegration separat, uden print:

1. Kopiér den fungerende `~/open-bamboo-persano/` inklusive build og eventuelle
   ABI-buildmapper til `/opt/open-bamboo-persano/`. Bevar originalen.
2. Lad servicebrugeren eje kopien (`sudo chown -R print-service:print-service
   /opt/open-bamboo-persano`). Wrapperen kan bygge/cache filer ved første kald.
   Systemd tillader kun skrivning i driverkopien og servicens datamappe.
3. Kopiér den fungerende certifikatopsætning til
   `/var/lib/print-service/config/cert/`, inklusive eventuelle tilhørende
   nøgler. Ejerskab print-service, mapper 0700 og filer 0600.
4. Kopiér den fungerende `bunny.params.json` til
   `/etc/print-service/print.params.json`, root:print-service og 0640.
   Bevar verificerede AMS-indstillinger og `try_emmc_print`. Fjern gamle
   hemmeligheder. API-klienter kan ikke ændre driverparametrene.
5. Udfyld BAMBU-værdier lokalt i miljøfilen. Kontroller paths og rettigheder.
   Lad `PRINT_MODE=simulate` stå under al udvikling og test.

Før senere fysisk brug skal den lokale repo-version og parameterfil gennemgås.
Adapteren følger den tidligere fungerende ABI 02.05.02/local_print-kommando.
Den overskriver filnavn, destination, identitet og opgavenavn pr. job og fjerner
gamle konfigurations-/checksumfelter. Den præcise Pi-version er endnu ikke tilgået
eller verificeret. Skift først til `PRINT_MODE=live`, når integrationen er godkendt
til rigtig brug. Et eksplicit startkald kan da starte printeren fysisk.

Driverens CLI kræver access code som argument. Den kan derfor kortvarigt ses i
proceslisten af root og andre lokale brugere afhængigt af /proc-opsætningen.
Brug en Pi med betroede lokale brugere. Servicen gemmer ingen rå driverlogs;
de kan indeholde hemmeligheder. Midlertidige parametre har 0600 og fjernes efter
kaldet. API-tokenet videregives ikke til driverens miljø.

## API

### Printertelemetri

Telemetri bruger Debian-pakken `python3-paho-mqtt` (2.x) og en selvstændig
MQTT/TLS-forbindelse. Den abonnerer kun på printerens report-topic og sender
kun en fast `pushall`-statusforespørgsel hvert 30. sekund. Den kalder aldrig
plugin_runner og ændrer ikke jobtilstande eller printindstillingen.
Certifikatkæden valideres mod Bambu CA; TLS kontrollerer serienummeret som
værtsnavn, før MQTT sender adgangskoden. Rå MQTT-data og hemmeligheder
returneres eller logges ikke.

På den aktuelle Pi er afhængigheden allerede installeret. Aktivér i Termius:

```sh
sudo python3 ~/PrintPlatform/deploy/configure-telemetry.py \
  --serial 22E8BJ652600933 --ip 192.168.0.191 \
  --ca /home/codeadmin/.config/BambuStudio/cert/printer.cer
```

Scriptet beder om printerens access code lokalt uden at vise den, kopierer CA'en,
opdaterer miljøfilen og genstarter servicen. Det kræver `PRINT_MODE=simulate`.
På en ny Pi installeres først `sudo apt install python3-paho-mqtt`.

`GET /v1/status` har nu et `printer`-objekt: `connected`, `state`, `last_seen`
(Unix-tid), `stale`, `error` og tilgængelige felter `progress_percent`,
`remaining_minutes`, `nozzle_c`, `bed_c`, `error_code` (rå printerfejlkode).
Delrapporter flettes; hvert felt udløber efter 90 sekunder. Afbrudt forbindelse
eller gammel tilstand giver `state=unknown`, aldrig et gættet IDLE.
`error` beskriver forbindelsen; `error_code` beskriver printet.
Beholdte MQTT-beskeder ignoreres. Status er ikke en fysisk sikkerhedskontrol.

Alle kald kræver `Authorization: Bearer <token>`. Bind er fast `127.0.0.1:8787`.
Der er ingen CORS-adgang. Brug SSH-tunnel under udvikling:

```sh
ssh -L 8787:127.0.0.1:8787 codeadmin@casmartRaspberry.local
```

Indlæs token privat i din lokale terminal, fx `read -rs API_TOKEN` i zsh/bash.

```sh
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8787/v1/status
curl -H "Authorization: Bearer $API_TOKEN" -H 'X-Filename: model.gcode.3mf' \
  --data-binary @model.gcode.3mf http://127.0.0.1:8787/v1/jobs
```

`GET /v1/jobs` viser køen. Brug det returnerede job-id i begge steder nedenfor.
Kaldet kræver, at operatøren har kontrolleret plade, filament og printerprofil:

```sh
curl -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"confirm_job_id":"JOB_ID","printer_ready":true}' \
  http://127.0.0.1:8787/v1/jobs/JOB_ID/start
```

Efter fysisk kontrol af færdig/tom printer:

```sh
curl -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"printer_checked_idle":true}' \
  http://127.0.0.1:8787/v1/jobs/JOB_ID/resolve
```

En fremtidig webapp skal have et backendlag med brugerlogin, som holder API-nøglen
server-side og bruger en privat, krypteret forbindelse til Pi'en. Eksponér ikke
Pi-porten på internettet. Systemd genstarter servicen efter fejl og ved boot.
Backup kræver stop af servicen og kopi af hele `/var/lib/print-service` samt
konfigurationen separat; dette indeholder følsomme data.

## Test uden printer

```sh
cd service
python3 -m unittest -v
```

Testene bruger midlertidigt lager og en lokal HTTP-server. Live-adaptertesten
erstatter processen med en mock; der køres aldrig plugin_runner. Kø, genstart,
autentificering, arkivafvisning og eksplicit start testes uden netværk til printeren.
Driverintegration og systemd skal efterfølgende verificeres på selve Pi'en.

Driverreference: https://github.com/persano/open-bamboo-networking/tree/master/tools/plugin_runner
