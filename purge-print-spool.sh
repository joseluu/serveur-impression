#!/bin/bash
# Purge le spool du serveur d'impression (~/serveur-impression/spool).
#
# Le serveur y depose une copie de chaque image envoyee a l'imprimante. Rien ne l'efface :
# sans purge, le repertoire grossit indefiniment.
#
# Ne sont supprimees que les images (.jpg / .png). Sont conserves :
#   - registre.json : il porte les pseudos et l'historique affiches par la page d'etat ;
#     une image purgee y reste referencee, seule sa vignette disparait (la page le gere).
#   - le journal des demandes, qui vit ailleurs (~/serveur-impression/journal/).
#
# Usage :
#   purge-print-spool.sh              supprime les images de plus de 7 jours
#   purge-print-spool.sh -j 2         ... de plus de 2 jours
#   purge-print-spool.sh -t           supprime toutes les images, quel que soit leur age
#   purge-print-spool.sh -n           simulation : montre ce qui serait supprime
set -u

SPOOL="${SPOOL:-$HOME/serveur-impression/spool}"
JOURS=7
TOUT=0
SIMULATION=0

usage() { sed -n '2,17p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

while getopts "j:tnh" opt; do
    case "$opt" in
        j) JOURS="$OPTARG" ;;
        t) TOUT=1 ;;
        n) SIMULATION=1 ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

if [ ! -d "$SPOOL" ]; then
    echo "Spool introuvable : $SPOOL" >&2
    exit 1
fi

if ! [[ "$JOURS" =~ ^[0-9]+$ ]]; then
    echo "Le nombre de jours doit etre un entier positif (recu : '$JOURS')" >&2
    exit 1
fi

# -mtime +N ne selectionne que les fichiers de plus de N jours ; avec -t on ne filtre pas.
if [ "$TOUT" -eq 1 ]; then
    critere=(-type f \( -iname '*.jpg' -o -iname '*.png' \))
    quoi="toutes les images"
else
    critere=(-type f \( -iname '*.jpg' -o -iname '*.png' \) -mtime "+$JOURS")
    quoi="les images de plus de $JOURS jour(s)"
fi

mapfile -t fichiers < <(find "$SPOOL" -maxdepth 1 "${critere[@]}" -print)

restants=$(find "$SPOOL" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.png' \) | wc -l)

if [ "${#fichiers[@]}" -eq 0 ]; then
    echo "Rien a purger dans $SPOOL ($quoi) — $restants image(s) au total."
    exit 0
fi

octets=0
for f in "${fichiers[@]}"; do
    octets=$((octets + $(stat -c %s "$f")))
done
lisible=$(numfmt --to=iec --suffix=o "$octets" 2>/dev/null || echo "$octets o")

if [ "$SIMULATION" -eq 1 ]; then
    echo "SIMULATION — seraient supprimees : ${#fichiers[@]} image(s), $lisible"
    printf '  %s\n' "${fichiers[@]}"
    exit 0
fi

rm -f -- "${fichiers[@]}"
echo "Purge de $SPOOL : ${#fichiers[@]} image(s) supprimee(s), $lisible liberes ($quoi)."
echo "Reste $((restants - ${#fichiers[@]})) image(s) dans le spool."
