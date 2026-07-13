# serveur-impression

Serveur d'impression pour borne photo : il reçoit une image par API avec le **pseudo du
demandeur**, l'envoie à l'imprimante, et expose une **page d'état** montrant la file d'attente,
l'état de l'imprimante, l'occupation du spool et un **journal des demandes**.

Pensé pour fonctionner avec [serveur-olympus](https://github.com/joseluu/serveur-olympus), qui
déclenche l'appareil photo : l'un prend la photo, l'autre l'imprime.

> **Matériel** — imprimante visée en production : **DNP DS820** (sublimation thermique, USB).
> Développé et testé avec une **HP LaserJet 3055** (USB). Le passage de l'une à l'autre ne
> demande **aucune modification du code** : voir [Changer d'imprimante](#changer-dimprimante).

## Le parti pris : CUPS *est* la file d'attente

Aucune file d'attente n'est réimplémentée par-dessus CUPS — deux files finiraient
inévitablement par diverger. Le serveur soumet le travail immédiatement via IPP (`pycups`) et
tient à jour un petit registre JSON qui associe chaque numéro de travail CUPS au pseudo du
demandeur et à l'image d'origine, les seules choses que CUPS ne sait pas stocker.

La page d'état **fusionne les deux sources** : l'état réel (imprimante, file, historique) est lu
par IPP, le pseudo et la vignette viennent du registre. Ce qui s'affiche est donc ce que CUPS
fait vraiment, pas ce que le serveur croit.

## Installation

```bash
sudo apt install cups cups-client python3-venv python3-dev libcups2-dev gcc
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`python3-dev`, `libcups2-dev` et `gcc` ne servent qu'à compiler `pycups`, qui est une extension C.

Puis déclarer l'imprimante dans CUPS (exemple avec la HP LaserJet 3055) :

```bash
lpinfo -v                                   # trouver l'URI USB de l'imprimante
sudo lpadmin -p HP3055 \
    -v 'usb://HP/LaserJet%203055?serial=XXXXXXXX' \
    -m drv:///hpcups.drv/hp-laserjet_3055.ppd -E
```

## Lancement

```bash
./venv/bin/python app.py        # écoute sur le port 5001
```

Page d'état : `http://<ip-de-la-machine>:5001/`

### En service permanent (systemd)

```ini
[Unit]
Description=Serveur d'impression (API de depot d'image + page d'etat)
After=network-online.target cups.service
Wants=network-online.target cups.service

[Service]
Type=exec
User=kiosque
WorkingDirectory=/home/kiosque/serveur-impression
Environment=IMPRIMANTE=HP3055
Environment=MEDIA=A4
ExecStart=/home/kiosque/serveur-impression/venv/bin/python /home/kiosque/serveur-impression/app.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/kiosque/serveur-impression/spool /home/kiosque/serveur-impression/journal

[Install]
WantedBy=multi-user.target
```

## Changer d'imprimante

Deux variables d'environnement, aucune ligne de code :

```bash
IMPRIMANTE=DS820 MEDIA=w288h432 ./venv/bin/python app.py
```

- `IMPRIMANTE` : le nom de la file CUPS (`lpstat -p`).
- `MEDIA` : le format papier CUPS (`A4`, `w288h432` pour du 4×6 pouces, etc. — voir
  `lpoptions -p <imprimante> -l`).

Le pilote de la **DNP DS820** est fourni par Gutenprint et se déclare ainsi :

```bash
sudo apt install printer-driver-gutenprint
sudo lpadmin -p DS820 -v 'usb://Dai%20Nippon%20Printing/DS820' \
    -m 'gutenprint.5.3://dnp-ds820/expert' -E
```

## API

### Déposer une image à imprimer

```
POST /api/impression        (multipart/form-data)
  image   : le fichier, JPEG ou PNG   (obligatoire)
  pseudo  : le nom du demandeur       (facultatif, « anonyme » par défaut)
```

```bash
curl -F 'image=@photo.jpg' -F 'pseudo=Camille' http://kiosque:5001/api/impression
# 202 {"numero":5,"pseudo":"Camille","imprimante":"HP3055","position":1,"en_attente":1}
```

Le format est vérifié par Pillow **sur le contenu réel**, pas sur l'extension ni sur le
`Content-Type`, qui sont déclaratifs : un `.jpg` qui n'en est pas un est refusé (`415`).
Taille maximale : 25 Mo (`413` au-delà).

### Autres routes

| Route | Rôle |
|---|---|
| `GET /` | page d'état : imprimante, file d'attente, spool, historique |
| `GET /api/etat` | même chose en JSON |
| `POST /api/travaux/<n>/annuler` | annule un travail |
| `GET /vignette/<n>` | aperçu de l'image déposée (404 si elle a été purgée) |
| `GET /journal?t=<code>` | journal des demandes — **protégé par un code** |
| `GET /api/journal?t=<code>` | le journal en JSON |

## Journal des demandes

Chaque demande d'impression ajoute une ligne à `journal/impressions.log` : date et heure,
numéro de travail, demandeur, nom du fichier, format, dimensions, taille, imprimante. Le journal
**survit à la purge du spool** — c'est la trace de qui a demandé quoi et quand.

Il est consultable à `/journal`, protégé par un **code d'accès** lu dans le fichier
**`code-acces.txt`**, à la racine du projet. Le code livré par défaut est `0E2FB2B4`.

Le fichier est relu à chaque requête : **changer le code ne demande pas de redémarrer** le
serveur, il suffit de réécrire `code-acces.txt`.

> **Ce code n'est pas un secret.** Il figure en clair dans ce dépôt public, et le paramètre `t`
> apparaît dans l'URL (donc dans l'historique du navigateur et les journaux d'accès). Il ne
> protège que d'une consultation fortuite par quelqu'un qui tomberait sur la page d'état — pas
> d'un curieux déterminé. Si le journal doit être réellement protégé, changez `code-acces.txt`
> pour une valeur qui ne soit pas publiée, et servez le tout derrière HTTPS.

## Spool et purge

Le serveur conserve dans `spool/` une copie de chaque image envoyée à l'imprimante. **Rien ne
l'efface tout seul** : la page d'état affiche en permanence le nombre de fichiers et leur poids
total, pour que la dérive se voie.

Le script `purge-print-spool.sh` fait le ménage. Sur le kiosque, il est atteignable depuis
`~/gestion/purge-print-spool.sh`, qui pointe (lien symbolique) sur celui du dépôt :

```bash
purge-print-spool.sh          # images de plus de 7 jours
purge-print-spool.sh -j 2     # ... de plus de 2 jours
purge-print-spool.sh -t       # toutes les images
purge-print-spool.sh -n       # simulation, ne supprime rien
```

Il ne supprime que les images. `registre.json` est conservé (sinon l'historique perdrait les
pseudos) et le journal vit ailleurs. Une image purgée reste donc dans l'historique, seule sa
vignette disparaît — la page le gère.

## Limites connues

- **Aucune authentification** sur le dépôt d'image : qui atteint le port 5001 peut lancer une
  impression. À réserver à un réseau de confiance.
- Le serveur de développement Flask est utilisé tel quel : suffisant pour une borne sur un
  réseau local, pas pour une exposition publique.
- L'image est envoyée telle quelle à CUPS avec `fit-to-page` ; aucun recadrage au format de
  l'imprimante n'est fait. À revoir pour la DS820 si le format photo doit être exactement rempli.
- Le registre ne fait que grossir (quelques centaines d'octets par travail).

## Licence

MIT.
