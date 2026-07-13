#!/usr/bin/env python3
"""Serveur d'impression : recoit une image par API, l'envoie a l'imprimante, expose l'etat.

L'imprimante visee en production est une **DNP DS820** (sublimation thermique, USB) ; le
developpement se fait avec une **HP LaserJet 3055** (USB). Le pilote de la DS820 est fourni
par Gutenprint (`gutenprint.5.3://dnp-ds820/expert`) : c'est le meme chemin CUPS, donc seule
la configuration change (voir IMPRIMANTE / MEDIA ci-dessous), pas le code.

C'est **CUPS qui fait la file d'attente** : on ne rajoute pas une file par-dessus, sinon les
deux divergeraient. Le serveur soumet le travail immediatement et tient a jour un registre qui
associe chaque numero de travail CUPS au pseudo du demandeur et a l'image d'origine, choses que
CUPS ne sait pas stocker. La page d'etat fusionne les deux : etat reel lu par IPP, pseudo lu
dans le registre.
"""

import hmac
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import cups
from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image
from werkzeug.exceptions import RequestEntityTooLarge

RACINE = Path(__file__).parent
SPOOL = RACINE / "spool"
REGISTRE = SPOOL / "registre.json"

# Journal des demandes : qui a demande quoi, et quand. Volontairement hors du spool, que
# purge-print-spool.sh vide regulierement.
JOURNAL = RACINE / "journal" / "impressions.log"

# Code d'acces au journal, dans un fichier a part pour etre changeable sans toucher au code.
# Relu a chaque appel : modifier le fichier suffit, pas besoin de redemarrer le serveur.
FICHIER_CODE = RACINE / "code-acces.txt"

# Imprimante cible, telle que nommee dans CUPS (`lpstat -p`).
#   - test        : HP3055    -> media A4
#   - production  : DS820     -> media au format DNP retenu
# Se surchargent sans toucher au code, via l'unite systemd ou l'environnement :
#   IMPRIMANTE=DS820 MEDIA=w288h432 ./venv/bin/python app.py
IMPRIMANTE = os.environ.get("IMPRIMANTE", "HP3055")
MEDIA = os.environ.get("MEDIA", "A4")

FORMATS_ACCEPTES = {"JPEG": ".jpg", "PNG": ".png"}
TAILLE_MAX = 25 * 1024 * 1024
HISTORIQUE_MAX = 30
JOURNAL_MAX = 500  # lignes affichees dans l'interface, les plus recentes d'abord

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = TAILLE_MAX
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Le registre et le journal sont lus a chaque affichage et ecrits a chaque depot : un verrou
# suffit, le volume reste minuscule.
_verrou = threading.Lock()

# Etats IPP d'un travail (RFC 8011).
ETATS_TRAVAIL = {
    3: ("en attente", "attente"),
    4: ("suspendu", "attente"),
    5: ("impression en cours", "encours"),
    6: ("arrete", "probleme"),
    7: ("annule", "probleme"),
    8: ("abandonne", "probleme"),
    9: ("termine", "termine"),
}
ETATS_IMPRIMANTE = {3: ("prete", "ok"), 4: ("impression en cours", "encours"), 5: ("arretee", "probleme")}


def connexion() -> cups.Connection:
    """Une connexion CUPS par appel : l'objet n'est pas sur en multi-fil, et le cout est nul
    (socket unix local)."""
    return cups.Connection()


def code_attendu() -> str:
    try:
        return FICHIER_CODE.read_text().strip()
    except OSError:
        app.logger.error("Fichier de code d'acces illisible : %s", FICHIER_CODE)
        return ""


def code_valide(fourni: Optional[str]) -> bool:
    attendu = code_attendu()
    # compare_digest : comparaison a temps constant, pour ne pas laisser deviner le code
    # caractere par caractere en chronometrant les reponses.
    return bool(attendu) and hmac.compare_digest((fourni or "").strip().upper(), attendu.upper())


def charge_registre() -> dict:
    if not REGISTRE.exists():
        return {}
    try:
        return json.loads(REGISTRE.read_text())
    except json.JSONDecodeError:
        app.logger.warning("Registre illisible, on repart de zero")
        return {}


def ecrit_registre(registre: dict) -> None:
    SPOOL.mkdir(parents=True, exist_ok=True)
    provisoire = REGISTRE.with_suffix(".tmp")
    provisoire.write_text(json.dumps(registre, indent=2, ensure_ascii=False))
    provisoire.replace(REGISTRE)  # remplacement atomique : jamais de registre tronque


def journalise(numero: int, pseudo: str, meta: dict) -> None:
    """Une ligne par demande d'impression, en champs separes par des tabulations."""
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    ligne = "\t".join([
        meta["recu"],
        str(numero),
        pseudo,
        meta["nom_origine"],
        meta["format"],
        f"{meta['dimensions'][0]}x{meta['dimensions'][1]}",
        str(meta["octets"]),
        IMPRIMANTE,
    ])
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def lit_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    lignes = JOURNAL.read_text(encoding="utf-8").splitlines()
    entrees = []
    for ligne in reversed(lignes[-JOURNAL_MAX:]):  # les plus recentes en tete
        champs = ligne.split("\t")
        if len(champs) != 8:
            continue
        recu, numero, pseudo, fichier, format_, dimensions, octets, imprimante = champs
        entrees.append({
            "recu": recu,
            "numero": int(numero) if numero.isdigit() else None,
            "pseudo": pseudo,
            "fichier": fichier,
            "format": format_,
            "dimensions": dimensions,
            "octets": int(octets) if octets.isdigit() else None,
            "imprimante": imprimante,
        })
    return entrees


def etat_spool() -> dict:
    """Nombre de fichiers et taille du spool, hors registre."""
    if not SPOOL.exists():
        return {"fichiers": 0, "octets": 0}
    images = [f for f in SPOOL.iterdir() if f.is_file() and f.suffix.lower() in (".jpg", ".png")]
    return {"fichiers": len(images), "octets": sum(f.stat().st_size for f in images)}


def nettoie_pseudo(brut: str) -> str:
    """Le pseudo finit dans le titre du travail CUPS, sur la page et dans le journal : on le borne."""
    pseudo = re.sub(r"[\x00-\x1f\x7f]", "", (brut or "").strip())
    return pseudo[:40] or "anonyme"


def valide_image(donnees: bytes) -> tuple[str, tuple[int, int]]:
    """Verifie que le contenu est bien un JPEG ou un PNG. Renvoie (format, dimensions).

    On se fie a Pillow, pas au Content-Type ni a l'extension, qui sont declaratifs.
    """
    try:
        with Image.open(io.BytesIO(donnees)) as img:
            img.verify()  # detecte un fichier tronque ou corrompu
        with Image.open(io.BytesIO(donnees)) as img:
            format_, dimensions = img.format, img.size
    except Exception as err:
        raise ValueError(f"Image illisible : {err}") from err
    if format_ not in FORMATS_ACCEPTES:
        raise ValueError(f"Format {format_} refuse (attendu : JPEG ou PNG)")
    return format_, dimensions


def etat_imprimante(conn: cups.Connection) -> dict:
    imprimantes = conn.getPrinters()
    if IMPRIMANTE not in imprimantes:
        return {
            "nom": IMPRIMANTE,
            "presente": False,
            "etat": "absente de CUPS",
            "classe": "probleme",
            "message": f"Aucune imprimante nommee '{IMPRIMANTE}'. Connues : "
            f"{', '.join(imprimantes) or 'aucune'}.",
            "raisons": [],
            "accepte": False,
        }
    p = imprimantes[IMPRIMANTE]
    libelle, classe = ETATS_IMPRIMANTE.get(p["printer-state"], ("etat inconnu", "probleme"))
    raisons = [r for r in p.get("printer-state-reasons", []) if r != "none"]

    # getPrinters() ne renvoie PAS 'printer-is-accepting-jobs' : il faut le demander
    # explicitement, sinon on croit a tort que l'imprimante refuse les travaux.
    try:
        accepte = bool(
            conn.getPrinterAttributes(
                IMPRIMANTE, requested_attributes=["printer-is-accepting-jobs"]
            ).get("printer-is-accepting-jobs", True)
        )
    except cups.IPPError:
        accepte = True  # dans le doute, on ne crie pas au loup

    return {
        "nom": IMPRIMANTE,
        "presente": True,
        "etat": libelle,
        # Une imprimante « prete » mais qui refuse les travaux n'est pas prete du tout :
        # toute nouvelle photo sera rejetee. La pastille doit le montrer.
        "classe": "probleme" if (raisons or not accepte) else classe,
        "message": p.get("printer-state-message") or "",
        "raisons": raisons,
        "accepte": accepte,
        "description": p.get("printer-info", ""),
        "connexion": p.get("device-uri", ""),
    }


def travaux(conn: cups.Connection) -> tuple[list, list]:
    """Renvoie (file d'attente, historique), enrichis avec le registre local."""
    registre = charge_registre()

    def enrichi(numero: int, attrs: dict, position: Optional[int] = None) -> dict:
        libelle, classe = ETATS_TRAVAIL.get(attrs.get("job-state", 0), ("inconnu", "probleme"))
        meta = registre.get(str(numero), {})
        fichier = meta.get("fichier")
        return {
            "numero": numero,
            "position": position,
            "pseudo": meta.get("pseudo", "?"),
            "fichier": meta.get("nom_origine", attrs.get("job-name", "?")),
            "dimensions": meta.get("dimensions"),
            "octets": meta.get("octets"),
            "recu": meta.get("recu"),
            # L'image a pu etre purgee du spool : la vignette n'existe alors plus.
            "vignette": bool(fichier) and (SPOOL / fichier).exists(),
            "etat": libelle,
            "classe": classe,
        }

    actifs = conn.getJobs(which_jobs="not-completed", requested_attributes=["job-id", "job-state", "job-name"])
    file = [enrichi(num, attrs, pos) for pos, (num, attrs) in enumerate(sorted(actifs.items()), start=1)]

    finis = conn.getJobs(which_jobs="completed", requested_attributes=["job-id", "job-state", "job-name"])
    historique = [enrichi(num, attrs) for num, attrs in sorted(finis.items(), reverse=True)][:HISTORIQUE_MAX]
    return file, historique


@app.route("/")
def page_etat():
    return render_template("etat.html")


@app.route("/api/etat")
def api_etat():
    conn = connexion()
    imprimante = etat_imprimante(conn)
    file, historique = travaux(conn) if imprimante["presente"] else ([], [])
    return jsonify({
        "imprimante": imprimante,
        "file": file,
        "historique": historique,
        "media": MEDIA,
        "spool": etat_spool(),
    })


@app.route("/api/impression", methods=["POST"])
def api_impression():
    """Recoit une image (JPEG ou PNG) et le pseudo du demandeur, lance l'impression.

    multipart/form-data : champ `image` (le fichier), champ `pseudo` (facultatif).
    """
    if "image" not in request.files:
        return jsonify({"erreur": "Champ 'image' manquant (multipart/form-data)."}), 400

    envoi = request.files["image"]
    donnees = envoi.read()
    if not donnees:
        return jsonify({"erreur": "Fichier vide."}), 400

    try:
        format_, dimensions = valide_image(donnees)
    except ValueError as err:
        return jsonify({"erreur": str(err)}), 415

    pseudo = nettoie_pseudo(request.form.get("pseudo", ""))

    conn = connexion()
    etat = etat_imprimante(conn)
    if not etat["presente"]:
        return jsonify({"erreur": etat["message"]}), 503

    SPOOL.mkdir(parents=True, exist_ok=True)
    chemin = SPOOL / f"{uuid.uuid4().hex}{FORMATS_ACCEPTES[format_]}"
    chemin.write_bytes(donnees)

    titre = f"{pseudo} - {envoi.filename or chemin.name}"
    try:
        numero = conn.printFile(
            IMPRIMANTE,
            str(chemin),
            titre,
            # fit-to-page : l'image est mise a l'echelle de la page sans deformation. Sur la
            # DS820 (format photo fixe), c'est ce qu'on veut aussi.
            {"fit-to-page": "true", "media": MEDIA},
        )
    except cups.IPPError as err:
        chemin.unlink(missing_ok=True)
        app.logger.error("Echec de la soumission a CUPS : %s", err)
        return jsonify({"erreur": f"CUPS a refuse le travail : {err}"}), 502

    meta = {
        "pseudo": pseudo,
        "nom_origine": envoi.filename or chemin.name,
        "fichier": chemin.name,
        "format": format_,
        "dimensions": list(dimensions),
        "octets": len(donnees),
        "recu": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with _verrou:
        registre = charge_registre()
        registre[str(numero)] = meta
        ecrit_registre(registre)
        journalise(numero, pseudo, meta)

    app.logger.info(
        "Travail %s : %s (%s, %dx%d, %d ko) de '%s'",
        numero, meta["nom_origine"], format_, *dimensions, len(donnees) // 1024, pseudo,
    )
    file, _ = travaux(conn)
    return jsonify({
        "numero": numero,
        "pseudo": pseudo,
        "imprimante": IMPRIMANTE,
        "position": next((t["position"] for t in file if t["numero"] == numero), None),
        "en_attente": len(file),
    }), 202


@app.route("/api/travaux/<int:numero>/annuler", methods=["POST"])
def api_annuler(numero: int):
    try:
        connexion().cancelJob(numero)
    except cups.IPPError as err:
        return jsonify({"erreur": f"Annulation impossible : {err}"}), 409
    app.logger.info("Travail %d annule", numero)
    return jsonify({"numero": numero, "annule": True})


@app.route("/vignette/<int:numero>")
def vignette(numero: int):
    """Apercu de l'image deposee, pour la page d'etat."""
    meta = charge_registre().get(str(numero))
    if not meta or not meta.get("fichier"):
        abort(404)
    chemin = SPOOL / meta["fichier"]
    if not chemin.exists():  # image purgee du spool
        abort(404)
    with Image.open(chemin) as img:
        img.thumbnail((240, 240))
        tampon = io.BytesIO()
        img.convert("RGB").save(tampon, "JPEG", quality=80)
    tampon.seek(0)
    return send_file(tampon, mimetype="image/jpeg")


@app.route("/journal")
def page_journal():
    """Journal des demandes, protege par le code d'acces (voir code-acces.txt)."""
    if not code_valide(request.args.get("t")):
        # 401 et pas 403 : l'acces est conditionne a un code, pas a une identite.
        return render_template("journal.html", autorise=False, entrees=[]), 401
    return render_template("journal.html", autorise=True, entrees=lit_journal())


@app.route("/api/journal")
def api_journal():
    if not code_valide(request.args.get("t")):
        return jsonify({"erreur": "Code d'acces invalide ou absent (parametre t)."}), 401
    return jsonify({"entrees": lit_journal()})


@app.errorhandler(RequestEntityTooLarge)
def trop_gros(_err):
    return jsonify({"erreur": f"Image trop lourde (maximum {TAILLE_MAX // (1024 * 1024)} Mo)."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, threaded=True)
