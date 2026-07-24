#!/usr/bin/env python3
"""Spouleur Windows presente sous la meme forme que le module `cups`.

Le serveur a ete ecrit pour CUPS. La machine Linux du kiosque etant hors service, il
tourne desormais sur Windows, ou CUPS n'existe pas. Plutot que de parsemer app.py de
tests de plateforme, ce module reimplemente **exactement** la petite surface de `cups`
qu'utilise le serveur -- cinq methodes et une exception -- au-dessus du spouleur Windows :

    import cups                      # Linux
    import spouleur_windows as cups  # Windows, meme code appelant

Les etats sont traduits vers ceux de la RFC 8011 (IPP), que app.py sait deja afficher :
c'est le vocabulaire commun, et il vaut mieux traduire ici, une fois, que partout ailleurs.

Difference qui remonte jusqu'a app.py : CUPS garde les travaux termines un certain temps,
le spouleur Windows les efface des qu'ils sortent de l'imprimante. getJobs("completed") ne
peut donc pas servir d'historique -- app.py le reconstruit a partir de son registre.
"""

import ctypes
import logging
import re
from ctypes import wintypes

import win32con
import win32gui
import win32print
from PIL import Image, ImageWin

journal = logging.getLogger(__name__)

# GDI est appele directement plutot qu'a travers win32ui : son StartDoc jette le numero
# de travail rendu par le systeme, alors que c'est lui qui relie une feuille au pseudo de
# celui qui l'a demandee. Le retrouver apres coup en fouillant la file serait une course
# perdue d'avance -- une photo 10x15 sort en quelques secondes.
_gdi = ctypes.WinDLL("gdi32", use_last_error=True)


class DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


# Sans argtypes, ctypes passerait le descripteur de contexte comme un entier 32 bits et le
# tronquerait : en 64 bits, GDI recevrait une poignee invalide et echouerait sans expliquer
# pourquoi. On declare donc les signatures.
_gdi.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(DOCINFOW)]
_gdi.StartDocW.restype = ctypes.c_int
for _nom_gdi in ("StartPage", "EndPage", "EndDoc", "AbortDoc"):
    getattr(_gdi, _nom_gdi).argtypes = [wintypes.HDC]
    getattr(_gdi, _nom_gdi).restype = ctypes.c_int
_gdi.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi.GetDeviceCaps.restype = ctypes.c_int
_gdi.DeleteDC.argtypes = [wintypes.HDC]
_gdi.DeleteDC.restype = wintypes.BOOL


class IPPError(Exception):
    """Meme nom que dans le module cups : app.py l'attrape sans savoir sur quoi il tourne."""


# --- traduction des etats ----------------------------------------------------------
# Bits de JOB_INFO_1.Status, du plus significatif au moins : le premier qui correspond
# gagne, car un travail annule et en erreur doit s'afficher "annule".
ETATS_TRAVAIL = [
    (0x00000100, 7),  # DELETED    -> annule
    (0x00000004, 7),  # DELETING   -> annule
    (0x00001000, 9),  # COMPLETE   -> termine
    (0x00000080, 9),  # PRINTED    -> termine
    (0x00000400, 6),  # USER_INTERVENTION -> arrete
    (0x00000040, 6),  # PAPEROUT   -> arrete
    (0x00000002, 6),  # ERROR      -> arrete
    (0x00000200, 6),  # BLOCKED_DEVQ -> arrete
    (0x00000020, 6),  # OFFLINE    -> arrete
    (0x00000001, 4),  # PAUSED     -> suspendu
    (0x00000010, 5),  # PRINTING   -> impression en cours
    (0x00000008, 5),  # SPOOLING   -> impression en cours
]

# Bits de PRINTER_INFO_2.Status qui constituent une *panne*, avec leur libelle.
# Volontairement limite aux vrais problemes : app.py considere toute raison presente
# comme un motif d'alerte, donc y mettre "en chauffe" ou "economie d'energie" ferait
# clignoter le voyant rouge sans raison.
PANNES_IMPRIMANTE = [
    (0x00000002, "erreur"),
    (0x00000008, "bourrage papier"),
    (0x00000010, "plus de papier"),
    (0x00000040, "probleme papier"),
    (0x00000080, "hors ligne"),
    (0x00000800, "bac de sortie plein"),
    (0x00001000, "indisponible"),
    (0x00040000, "plus de consommable"),
    (0x00100000, "intervention requise"),
    (0x00200000, "memoire saturee"),
    (0x00400000, "capot ouvert"),
    (0x00800000, "serveur d'impression injoignable"),
    (0x00000004, "suppression en cours"),
]

PRINTER_STATUS_PAUSED = 0x00000001
PRINTER_STATUS_PRINTING = 0x00000400
PRINTER_STATUS_PROCESSING = 0x00004000
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400

# DeviceCapabilities : formats papier connus du pilote.
DC_PAPERS = 2
DC_PAPERSIZE = 3
DC_PAPERNAMES = 16

POINTS_PAR_POUCE = 72.0
# Tolerance d'appariement d'un format papier, en dixiemes de millimetre (soit 5 mm).
# Deux ecarts se cumulent : l'arrondi des pilotes, et surtout le debord dye-sub. Le vrai
# pilote DS620 annonce son 4x6 a 104,9x156,1 mm, pas a 101,6x152,4 (w288h432) : l'impression
# sans marge deborde de ~3-4 mm pour garantir un plein bord apres decoupe. Une tolerance de
# 1,5 mm faisait donc echouer l'appariement et retomber sur le format par defaut du pilote.
# 5 mm absorbe ce debord tout en restant tres en dessous de l'ecart vers le format voisin le
# plus proche (>15 mm sur la grille DS620), donc le 4x6 reste seul a correspondre.
TOLERANCE_FORMAT = 50


def _etat_travail(status: int) -> int:
    for bit, etat in ETATS_TRAVAIL:
        if status & bit:
            return etat
    return 3  # en attente : le spouleur n'a encore rien signale


def _dimensions_media(media: str):
    """Traduit un nom de media CUPS en (largeur, hauteur) en dixiemes de millimetre.

    On garde la notation CUPS `w<largeur>h<hauteur>` en points PostScript (w288h432 =
    4x6 pouces) : c'est la valeur deja presente dans la configuration du service, et la
    changer en migrant de plateforme serait une source d'erreur de plus le jour J.
    """
    connus = {"a4": (2100, 2970), "letter": (2159, 2794), "a6": (1050, 1480)}
    if media.lower() in connus:
        return connus[media.lower()]
    trouve = re.fullmatch(r"w(\d+)h(\d+)", media.strip(), re.IGNORECASE)
    if not trouve:
        return None
    points = (int(trouve.group(1)), int(trouve.group(2)))
    return tuple(round(p / POINTS_PAR_POUCE * 254) for p in points)


def _code_papier(nom: str, port: str, media: str):
    """Cherche dans le pilote le format papier correspondant a `media`.

    Renvoie (code DEVMODE, libelle) ou None si le pilote n'a rien d'approchant, auquel
    cas on laissera le format par defaut de l'imprimante -- mieux vaut imprimer avec le
    reglage du pilote que refuser d'imprimer.
    """
    voulu = _dimensions_media(media)
    if voulu is None:
        journal.warning("Format media '%s' non reconnu, on garde le defaut du pilote", media)
        return None
    try:
        codes = win32print.DeviceCapabilities(nom, port, DC_PAPERS)
        tailles = win32print.DeviceCapabilities(nom, port, DC_PAPERSIZE)
        libelles = win32print.DeviceCapabilities(nom, port, DC_PAPERNAMES)
    except Exception:
        journal.warning("Formats papier illisibles pour '%s'", nom, exc_info=True)
        return None

    for code, taille, libelle in zip(codes, tailles, libelles):
        # DeviceCapabilities rend les tailles sous forme de dictionnaires {'x':.., 'y':..},
        # en dixiemes de millimetre.
        largeur, hauteur = taille["x"], taille["y"]
        # Le papier peut etre decrit dans un sens ou dans l'autre : c'est la meme feuille.
        for l_voulu, h_voulu in (voulu, voulu[::-1]):
            if abs(largeur - l_voulu) <= TOLERANCE_FORMAT and abs(hauteur - h_voulu) <= TOLERANCE_FORMAT:
                return code, libelle.rstrip("\x00")
    journal.warning(
        "Aucun format papier du pilote '%s' ne correspond a %s (%.1fx%.1f mm), "
        "on garde le defaut", nom, media, voulu[0] / 10, voulu[1] / 10,
    )
    return None


class Connection:
    """Equivalent de cups.Connection. Sans etat : chaque appel interroge le spouleur."""

    # --- imprimantes ---------------------------------------------------------------
    def getPrinters(self) -> dict:
        try:
            brut = win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS, None, 2
            )
        except Exception as err:
            raise IPPError(f"Spouleur Windows injoignable : {err}") from err

        imprimantes = {}
        for p in brut:
            status = p["Status"]
            raisons = [libelle for bit, libelle in PANNES_IMPRIMANTE if status & bit]
            if p["Attributes"] & PRINTER_ATTRIBUTE_WORK_OFFLINE and "hors ligne" not in raisons:
                raisons.append("hors ligne")

            if raisons:
                etat = 5  # arretee
            elif status & (PRINTER_STATUS_PRINTING | PRINTER_STATUS_PROCESSING) or p["cJobs"]:
                etat = 4  # impression en cours
            elif status & PRINTER_STATUS_PAUSED:
                etat = 5
            else:
                etat = 3  # prete

            imprimantes[p["pPrinterName"]] = {
                "printer-state": etat,
                "printer-state-reasons": raisons or ["none"],
                "printer-state-message": ", ".join(raisons),
                "printer-info": p["pComment"] or p["pDriverName"] or p["pPrinterName"],
                # Pas d'URI au sens CUPS sous Windows : le port joue le meme role pour
                # l'exploitant, c'est-a-dire repondre a "elle est branchee ou ?".
                "device-uri": f"{p['pPortName']} ({p['pDriverName']})",
                "_status": status,
                "_attributes": p["Attributes"],
                "_port": p["pPortName"],
            }
        return imprimantes

    def getPrinterAttributes(self, nom: str, requested_attributes=None) -> dict:
        imprimantes = self.getPrinters()
        if nom not in imprimantes:
            raise IPPError(f"Imprimante inconnue : {nom}")
        p = imprimantes[nom]
        # Windows n'expose pas "accepte les travaux" : une imprimante en pause est le
        # seul cas ou le spouleur retient les travaux au lieu de les transmettre.
        return {
            "printer-is-accepting-jobs": not (p["_status"] & PRINTER_STATUS_PAUSED),
        }

    # --- travaux --------------------------------------------------------------------
    def getJobs(self, which_jobs: str = "not-completed", requested_attributes=None) -> dict:
        termines = which_jobs == "completed"
        travaux = {}
        for nom in self.getPrinters():
            handle = win32print.OpenPrinter(nom)
            try:
                # -1 : tous les travaux de la file, sans avoir a la compter d'abord.
                lot = win32print.EnumJobs(handle, 0, -1, 1)
            except Exception:
                journal.warning("File d'attente illisible pour '%s'", nom, exc_info=True)
                continue
            finally:
                win32print.ClosePrinter(handle)
            for j in lot:
                etat = _etat_travail(j["Status"])
                if (etat in (7, 9)) != termines:
                    continue
                travaux[j["JobId"]] = {
                    "job-id": j["JobId"],
                    "job-state": etat,
                    "job-name": j["pDocument"] or "",
                }
        return travaux

    def cancelJob(self, numero: int) -> None:
        for nom in self.getPrinters():
            handle = win32print.OpenPrinter(nom)
            try:
                travaux = {j["JobId"] for j in win32print.EnumJobs(handle, 0, -1, 1)}
                if numero in travaux:
                    win32print.SetJob(handle, numero, 0, None, win32print.JOB_CONTROL_DELETE)
                    return
            except Exception as err:
                raise IPPError(f"Annulation du travail {numero} impossible : {err}") from err
            finally:
                win32print.ClosePrinter(handle)
        raise IPPError(f"Travail {numero} introuvable dans la file")

    # --- impression -------------------------------------------------------------------
    def printFile(self, nom: str, chemin: str, titre: str, options: dict, sortie: str = None) -> int:
        """Imprime une image et renvoie le numero de travail attribue par le spouleur.

        CUPS savait interpreter un JPEG tout seul ; le spouleur Windows, non : il attend
        du dessin. On ouvre donc un contexte d'impression et on y dessine l'image, ce qui
        a l'avantage de nous rendre maitres du cadrage au lieu de le subir.

        `sortie` detourne le travail vers un fichier au lieu du papier : sert a valider la
        chaine sans consommer de ruban, l'article le plus rare le jour de l'evenement.
        """
        imprimantes = self.getPrinters()
        if nom not in imprimantes:
            raise IPPError(f"Imprimante inconnue : {nom}. Connues : {', '.join(imprimantes) or 'aucune'}")

        hdc = self._contexte(nom, imprimantes[nom]["_port"], options.get("media", ""))
        try:
            info = DOCINFOW()
            info.cbSize = ctypes.sizeof(DOCINFOW)
            info.lpszDocName = titre
            info.lpszOutput = sortie
            numero = _gdi.StartDocW(hdc, ctypes.byref(info))
            if numero <= 0:
                raise IPPError(f"Le spouleur a refuse le travail (erreur {ctypes.get_last_error()})")
            try:
                if _gdi.StartPage(hdc) <= 0:
                    raise IPPError("Ouverture de la page impossible")
                self._dessine(hdc, chemin)
                if _gdi.EndPage(hdc) <= 0:
                    raise IPPError("Fermeture de la page impossible")
            except BaseException:
                # Un document commence et jamais termine laisse un travail fantome en tete
                # de file, qui bloque tous les suivants. On l'abandonne franchement.
                _gdi.AbortDoc(hdc)
                raise
            if _gdi.EndDoc(hdc) <= 0:
                raise IPPError("Envoi du document au spouleur impossible")
        except IPPError:
            raise
        except Exception as err:
            raise IPPError(f"Impression impossible : {err}") from err
        finally:
            _gdi.DeleteDC(hdc)
        return numero

    def _contexte(self, nom: str, port: str, media: str) -> int:
        """Ouvre un contexte d'impression au format papier demande, et rend son descripteur.

        Si le pilote ne connait pas le format, on imprime quand meme avec son reglage par
        defaut : une photo au mauvais format vaut mieux que pas de photo du tout, et
        l'avertissement est journalise.
        """
        papier = _code_papier(nom, port, media) if media else None
        devmode = None
        if papier is not None:
            code, libelle = papier
            handle = win32print.OpenPrinter(nom)
            try:
                devmode = win32print.GetPrinter(handle, 2)["pDevMode"]
            finally:
                win32print.ClosePrinter(handle)
            devmode.PaperSize = code
            devmode.Fields |= win32con.DM_PAPERSIZE
            journal.info("Format papier '%s' (code %d) retenu pour %s", libelle, code, media)

        hdc = win32gui.CreateDC("WINSPOOL", nom, devmode)
        if not hdc:
            raise IPPError(f"Contexte d'impression impossible a ouvrir pour '{nom}'")
        return hdc

    @staticmethod
    def _dessine(hdc: int, chemin: str) -> None:
        """Dessine l'image sur la page entiere, sans la deformer.

        Equivalent de l'option CUPS `fit-to-page` qu'utilisait la version Linux : mise a
        l'echelle en conservant les proportions, puis centrage. Une photo 3:2 sur un
        10x15 (3:2 lui aussi) remplit donc exactement la feuille.

        L'image est pivotee si son orientation ne correspond pas a celle de la page :
        sans cela, une photo paysage sur une page portrait n'occuperait qu'une bande
        centrale, avec deux larges marges blanches -- et une DS620 ne rend pas le ruban.
        """
        page_l = _gdi.GetDeviceCaps(hdc, win32con.HORZRES)
        page_h = _gdi.GetDeviceCaps(hdc, win32con.VERTRES)

        with Image.open(chemin) as brut:
            image = brut.convert("RGB")
            if (image.width > image.height) != (page_l > page_h) and image.width != image.height:
                image = image.rotate(90, expand=True)

            # Arrondi et non troncature : sur une photo qui tombe juste, tronquer laisserait
            # un lisere blanc d'un pixel sur un bord, faute d'un centieme de pixel.
            echelle = min(page_l / image.width, page_h / image.height)
            large = max(1, round(image.width * echelle))
            haut = max(1, round(image.height * echelle))
            x = (page_l - large) // 2
            y = (page_h - haut) // 2
            ImageWin.Dib(image).draw(hdc, (x, y, x + large, y + haut))
