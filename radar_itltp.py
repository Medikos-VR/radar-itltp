#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
=============================================================================
RADAR CHANTIERS I.T.L.T.P. (Igny - 91) — Étape 1.2 (Version avec Devis PDF Conforme)
Détection automatique de chantiers BTP en Île-de-France (Location avec chauffeur)
Sources : API BOAMP v2.1 + Open Data Sitadel (data.gouv.fr) + API Annuaire Entreprises
=============================================================================
"""

import csv
import html
import io
import json
import logging
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import smtplib
from email.message import EmailMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Radar_ITLTP")

# =============================================================================
# 1. CONFIGURATION MÉTIER I.T.L.T.P. (Igny - 91430)
# =============================================================================

DEPS_COEUR = {"91"}
DEPS_PROCHES = {"92", "78", "94"}
DEPS_GRANDE_IDF = {"75", "77", "93", "95"}
TOUS_DEPS_IDF = DEPS_COEUR | DEPS_PROCHES | DEPS_GRANDE_IDF

COMMUNES_PROCHES_IGNY = {
    "IGNY", "PALAISEAU", "MASSY", "VAUHALLAN", "SACLAY", "ORSAY", "BIEVRES",
    "VERRIERES-LE-BUISSON", "CHILLY-MAZARIN", "CHAMPLAN", "LONGJUMEAU", "WISSOUS",
    "ANTONY", "CHATENAY-MALABRY", "CLAMART", "LE PLESSIS-ROBINSON", "VELIZY-VILLACOUBLAY",
    "JOUY-EN-JOSAS", "LES LOGES-EN-JOSAS", "BUC", "GIF-SUR-YVETTE", "BURES-SUR-YVETTE",
    "VILLEBON-SUR-YVETTE", "VILLEJUST", "COURTACOUBOEUVRE", "LES ULIS", "FRESNES", "RUNGIS",
}

REGLES_MATERIEL = [
    {
        "nom": "Démolition / Gros curage",
        "mots_cles": ["demolition", "démolition", "deconstruction", "déconstruction", "curage", "desamiantage"],
        "materiel": "Pelle Liebherr (926/934/936) + 4 Bennes (évacuation) + Porte-char",
        "points": 35,
        "lourd": True,
    },
    {
        "nom": "Gros Terrassement / ZAC / Lotissement",
        "mots_cles": [
            "terrassement", "mouvement de terre", "fouille", "excavation", "remblai",
            "deblai", "déblai", "zac", "lotissement", "plateforme", "bassin de retention",
        ],
        "materiel": "Pelles Liebherr (926/934/936) + Chargeuse chenilles 63C + Bennes avec chauffeur",
        "points": 35,
        "lourd": True,
    },
    {
        "nom": "VRD / Assainissement / Réseaux / Voirie",
        "mots_cles": [
            "vrd", "voirie", "reseaux divers", "réseaux divers", "assainissement",
            "tranchee", "tranchée", "canalisation", "eau potable", "eaux pluviales", "enrobe", "chaussée",
        ],
        "materiel": "Mecalac / Mini-pelles Hyundai + Chargeuse pneus + Bennes avec chauffeur",
        "points": 25,
        "lourd": False,
    },
    {
        "nom": "Viabilité hivernale / Sablage / Salage",
        "mots_cles": ["deneigement", "déneigement", "salage", "sablage", "viabilite hivernale", "verglas"],
        "materiel": "Saleuse avec chauffeur",
        "points": 30,
        "lourd": False,
    },
    {
        "nom": "Transfert d'engins / Convoi",
        "mots_cles": ["porte-char", "porte char", "transfert d'engins", "transport d'engins", "convoi exceptionnel"],
        "materiel": "Porte-char avec chauffeur",
        "points": 30,
        "lourd": True,
    },
]

MOTS_EXCLUS = [
    "peinture", "menuiserie", "plomberie", "electricite", "électricité", "nettoyage des locaux",
    "gardiennage", "fourniture de bureau", "restauration scolaire", "logiciel", "assurance",
    "ascenseur", "chauffage", "climatisation", "serrurerie", "carrelage", "espaces verts tonte",
]

URL_API_BOAMP = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
URL_DATASET_SITADEL = "https://www.data.gouv.fr/api/1/datasets/liste-des-permis-de-construire-et-autres-autorisations-durbanisme/"
URL_API_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"

FICHIER_CSV = "radar_itltp_chantiers.csv"
FICHIER_HTML = "index.html"
FICHIER_SAUVEGARDE = "sauvegarde_historique_chantiers.json"


# =============================================================================
# 2. FONCTIONS UTILITAIRES ET MOTEUR DE SCORING
# =============================================================================

def normaliser_texte(texte: str) -> str:
    if not texte:
        return ""
    return re.sub(r"\s+", " ", str(texte).strip())

def qualifier_chantier(texte_analyse: str, code_dep: str, commune: str, est_attribue: bool, surface_m2: float = 0) -> dict:
    texte_min = normaliser_texte(texte_analyse).lower()
    commune_maj = normaliser_texte(commune).upper()

    for mot_exclu in MOTS_EXCLUS:
        if mot_exclu in texte_min and not any(
            m in texte_min for m in ["terrassement", "vrd", "demolition", "démolition", "voirie", "assainissement"]
        ):
            return {"pertinent": False, "score": 0, "raisons": ["Lot hors TP"], "materiel": "", "lourd": False}

    score = 0
    raisons = []
    materiels_detectes = []
    est_chantier_lourd = False

    for regle in REGLES_MATERIEL:
        if any(mot in texte_min for mot in regle["mots_cles"]):
            score += regle["points"]
            raisons.append(regle["nom"])
            materiels_detectes.append(regle["materiel"])
            if regle["lourd"]:
                est_chantier_lourd = True

    if not materiels_detectes and surface_m2 >= 1000:
        score += 25
        raisons.append(f"Gros projet ({int(surface_m2)} m²)")
        materiels_detectes.append("Pelles Liebherr (926/934/936) + Bennes + Porte-char avec chauffeur")
        est_chantier_lourd = True

    if score == 0:
        return {"pertinent": False, "score": 0, "raisons": [], "materiel": "", "lourd": False}

    if any(ville in commune_maj for ville in COMMUNES_PROCHES_IGNY):
        score += 30
        raisons.append("À < 20 min d'Igny")
    elif code_dep in DEPS_COEUR:
        score += 20
        raisons.append("Essonne (91)")
    elif code_dep in DEPS_PROCHES:
        score += 12
        raisons.append(f"Proche Igny ({code_dep})")
    elif code_dep in DEPS_GRANDE_IDF:
        if not est_chantier_lourd and surface_m2 < 1500:
            return {"pertinent": False, "score": 0, "raisons": ["Trop loin pour petit matériel"], "materiel": "", "lourd": False}
        score += 5
        raisons.append(f"IDF ({code_dep}) - Chantier lourd")

    if est_attribue:
        score += 20
        raisons.append("Marché déjà attribué")

    score_final = min(100, score)
    materiel_principal = materiels_detectes[0] if materiels_detectes else "Mini-pelles Hyundai / Mecalac / Bennes avec chauffeur"

    return {
        "pertinent": score_final >= 40,
        "score": score_final,
        "raisons": raisons,
        "materiel": materiel_principal,
        "lourd": est_chantier_lourd,
    }

def generer_accroche_telephonique(entreprise: str, statut: str, commune: str, objet: str, materiel: str) -> str:
    objet_court = normaliser_texte(objet)[:70] + ("..." if len(normaliser_texte(objet)) > 70 else "")
    if statut == "Confirmé":
        return (
            f"« Bonjour, je demande le conducteur de travaux en charge du chantier sur {commune}. "
            f"Je suis le gérant d'I.T.L.T.P. à Igny (91). J'ai vu que vous aviez remporté le marché "
            f"({objet_court}). Nous avons notre dépôt tout proche avec {materiel.lower()} disponibles "
            f"avec chauffeur et notre propre porte-char en cas de renfort ou pic d'activité. »"
        )
    else:
        return (
            f"« Bonjour, je suis le gérant d'I.T.L.T.P., loueur d'engins TP avec chauffeur basé à Igny (91). "
            f"Concernant votre projet sur {commune} ({objet_court}), avez-vous déjà désigné le lot "
            f"terrassement/démolition, ou cherchez-vous des pelles Liebherr, mini-pelles et bennes avec chauffeur ? »"
        )


# =============================================================================
# 3. ENRICHISSEMENT ENTREPRISE
# =============================================================================

CACHE_ENTREPRISES = {}

def enrichir_entreprise_publique(nom_ou_siret: str) -> dict:
    cle = normaliser_texte(nom_ou_siret).upper()
    if not cle or cle in ("À IDENTIFIER SUR PLACE / MAIRIE", "NON RENSEIGNÉ"):
        return {"nom_officiel": nom_ou_siret, "siren": "", "ville_siege": "", "lien_annuaire": ""}
    if cle in CACHE_ENTREPRISES:
        return CACHE_ENTREPRISES[cle]
    try:
        resp = requests.get(URL_API_ENTREPRISES, params={"q": cle, "per_page": 1}, timeout=5)
        if resp.status_code == 200:
            resultats = resp.json().get("results", [])
            if resultats:
                ent = resultats[0]
                siren = ent.get("siren", "")
                nom_officiel = ent.get("nom_complet", nom_ou_siret)
                siege = ent.get("siege", {})
                ville_siege = siege.get("libelle_commune", "")
                info = {
                    "nom_officiel": nom_officiel, "siren": siren, "ville_siege": ville_siege,
                    "lien_annuaire": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}" if siren else "",
                }
                CACHE_ENTREPRISES[cle] = info
                return info
    except Exception:
        pass
    info_defaut = {"nom_officiel": nom_ou_siret, "siren": "", "ville_siege": "", "lien_annuaire": ""}
    CACHE_ENTREPRISES[cle] = info_defaut
    return info_defaut


# =============================================================================
# 4. COLLECTEURS BOAMP ET SITADEL
# =============================================================================

def extraire_titulaire_boamp(record: dict) -> str:
    titulaire = record.get("titulaire")
    if titulaire:
        if isinstance(titulaire, list):
            return ", ".join(str(t) for t in titulaire if t)
        return str(titulaire)
    donnees_brutes = record.get("donnees")
    if donnees_brutes:
        match = re.search(r'"(?:titulaire|nomOfficiel|denomination)"\s*:\s*"([^"]{3,80})"', str(donnees_brutes), re.IGNORECASE)
        if match:
            return match.group(1)
    return ""

def descripteur_str(rec: dict) -> str:
    desc = rec.get("descripteur_libelle", "")
    if isinstance(desc, list):
        return " ".join(str(x) for x in desc)
    return str(desc or "")

def collecter_boamp(jours_historique: int = 45) -> tuple[list[dict], str]:
    logger.info("Interrogation de l'API BOAMP...")
    date_min = (datetime.now() - timedelta(days=jours_historique)).strftime("%Y-%m-%d")
    deps_str = ", ".join(f"'{d}'" for d in sorted(TOUS_DEPS_IDF))
    requete_where = (
        f"dateparution >= date'{date_min}' AND code_departement IN ({deps_str}) AND (search(objet, 'terrassement') "
        f"OR search(objet, 'démolition') OR search(objet, 'VRD') OR search(objet, 'voirie') OR search(objet, 'assainissement') "
        f"OR search(objet, 'fouille') OR search(objet, 'déneigement') OR search(objet, 'lotissement'))"
    )
    chantiers_boamp = []
    try:
        reponse = requests.get(URL_API_BOAMP, params={"where": requete_where, "order_by": "dateparution DESC", "limit": 100}, timeout=15)
        reponse.raise_for_status()
        enregistrements = reponse.json().get("results", [])
        logger.info(f"BOAMP : {len(enregistrements)} annonces récupérées.")
        for rec in enregistrements:
            idweb = rec.get("idweb", "")
            objet = normaliser_texte(rec.get("objet", ""))
            nature = normaliser_texte(rec.get("nature_libelle") or rec.get("nature") or "")
            acheteur = normaliser_texte(rec.get("nomacheteur", "Acheteur public"))
            date_parution = rec.get("dateparution", "")[:10]
            code_dep = rec.get("code_departement", "")
            if isinstance(code_dep, list):
                code_dep = "91" if "91" in code_dep else (code_dep[0] if code_dep else "")
            code_dep = str(code_dep)

            titulaire_brut = extraire_titulaire_boamp(rec)
            est_attribue = bool(titulaire_brut) or ("attribution" in nature.lower()) or ("résultat" in nature.lower())

            Qualification = qualifier_chantier(
                texte_analyse=f"{objet} {descripteur_str(rec)}", code_dep=code_dep,
                commune=f"{acheteur} {objet}", est_attribue=est_attribue,
            )

            if not Qualification["pertinent"]: continue

            statut = "Confirmé" if titulaire_brut else "À vérifier"
            cible = titulaire_brut if titulaire_brut else acheteur
            role_cible = "Entreprise BTP attributaire" if titulaire_brut else f"Donneur d'ordre ({nature or 'Appel d offres'})"

            chantiers_boamp.append({
                "id_unique": f"BOAMP-{idweb or hash(objet)}", "source": f"BOAMP ({nature or 'Marché Public'})",
                "date_publication": date_parution, "statut_fiabilite": statut, "score": Qualification["score"],
                "raisons_score": " + ".join(Qualification["raisons"]), "departement": code_dep,
                "commune_adresse": f"[{code_dep}] {acheteur}", "entreprise_cible": cible, "role_entreprise": role_cible,
                "description_travaux": objet, "engin_probable": Qualification["materiel"],
                "accroche_telephonique": generer_accroche_telephonique(cible, statut, acheteur, objet, Qualification["materiel"]),
                "lien_officiel": f"https://www.boamp.fr/pages/avis/?q=idweb:%22{idweb}%22" if idweb else "https://www.boamp.fr",
            })
        return chantiers_boamp, "OK"
    except Exception as e:
        logger.error(f"Erreur BOAMP: {e}")
        return [], str(e)

def extraire_lignes_sitadel(lecteur, type_permis_label, est_demolition, annee_min, resultat_list, max_lignes):
    compteur = 0
    for row in lecteur:
        compteur += 1
        if compteur > max_lignes: break
        dep = (row.get("DEP") or row.get("DEP_CODE") or "").strip()
        if dep not in (DEPS_COEUR | DEPS_PROCHES): continue
        cat_dem, denom_dem = (row.get("CAT_DEM") or "").strip(), normaliser_texte(row.get("DENOM_DEM") or "")
        siren_dem = normaliser_texte(row.get("SIREN_DEM") or row.get("SIRET_DEM") or "")
        if cat_dem == "10" or not denom_dem: continue
        date_aut = (row.get("DATE_REELLE_AUTORISATION") or row.get("DATE_REELLE_DOC") or "").strip()
        if date_aut and date_aut[:4] < annee_min: continue
        num_dau = normaliser_texte(row.get("NUM_DAU") or "")
        commune = normaliser_texte(row.get("ADR_LOCALITE_TER") or row.get("COMM") or f"Département {dep}")
        adresse_complete = " ".join(filter(None, [normaliser_texte(row.get("ADR_NUM_TER") or ""), normaliser_texte(row.get("ADR_TYPEVOIE_TER") or ""), normaliser_texte(row.get("ADR_LIBVOIE_TER") or ""), commune]))
        try: surface = float(row.get("SURF_LOC_CREEE") or row.get("SUPERFICIE_TERRAIN") or 0)
        except ValueError: surface = 0.0
        mots_nature = "démolition curage évacuation bennes" if est_demolition else "terrassement lotissement VRD aménagement"
        desc_travaux = f"{type_permis_label} n°{num_dau} ({int(surface)} m²)" if surface > 0 else f"{type_permis_label} n°{num_dau}"

        qualif = qualifier_chantier(texte_analyse=f"{mots_nature} {desc_travaux}", code_dep=dep, commune=commune, est_attribue=False, surface_m2=surface)
        if not qualif["pertinent"]: continue

        resultat_list.append({
            "id_unique": f"SITADEL-{num_dau or hash(adresse_complete)}", "source": type_permis_label,
            "date_publication": date_aut or "Récent", "statut_fiabilite": "À vérifier", "score": qualif["score"],
            "raisons_score": " + ".join(qualif["raisons"]), "departement": dep, "commune_adresse": f"[{dep}] {adresse_complete}",
            "entreprise_cible": f"{denom_dem}" + (f" (SIREN {siren_dem})" if siren_dem else ""), "role_entreprise": "Demandeur du permis",
            "description_travaux": desc_travaux, "engin_probable": qualif["materiel"],
            "accroche_telephonique": generer_accroche_telephonique(denom_dem, "À vérifier", commune, desc_travaux, qualif["materiel"]),
            "lien_officiel": "https://www.data.gouv.fr/datasets/liste-des-permis-de-construire-et-autres-autorisations-durbanisme",
        })

def collecter_sitadel(max_lignes_analysees: int = 80000) -> tuple[list[dict], str]:
    logger.info("Interrogation SITADEL...")
    chantiers_sitadel = []
    try:
        resp_meta = requests.get(URL_DATASET_SITADEL, timeout=15)
        resp_meta.raise_for_status()
        ressources = resp_meta.json().get("resources", [])
        cibles_csv = []
        for res in ressources:
            titre, fmt, url_res = (res.get("title") or "").lower(), (res.get("format") or "").lower(), res.get("url") or ""
            if ("csv" in fmt or url_res.endswith(".csv") or url_res.endswith(".zip")) and ("aménager" in titre or "amenager" in titre or "démolir" in titre or "demolir" in titre):
                cibles_csv.append((res.get("title", "Sitadel CSV"), url_res))
        if not cibles_csv: return [], "SITADEL : Aucun fichier CSV."
        annee_min = str(datetime.now().year - 1)
        for titre_fichier, url_fichier in cibles_csv[:2]:
            est_demolition = "démolir" in titre_fichier.lower() or "demolir" in titre_fichier.lower()
            type_permis_label = "Permis de Démolir (Sitadel)" if est_demolition else "Permis d'Aménager / Lotissement (Sitadel)"
            try:
                with requests.get(url_fichier, stream=True, timeout=25) as flux:
                    flux.raise_for_status()
                    if not url_fichier.lower().endswith(".zip"):
                        lecteur = csv.DictReader((ligne.decode("utf-8", errors="replace") for ligne in flux.iter_lines()), delimiter=";")
                        extraire_lignes_sitadel(lecteur, type_permis_label, est_demolition, annee_min, chantiers_sitadel, max_lignes_analysees)
                    else:
                        with zipfile.ZipFile(io.BytesIO(flux.content)) as zf:
                            nom_csv = [n for n in zf.namelist() if n.endswith(".csv")][0]
                            with zf.open(nom_csv) as f_csv:
                                lecteur = csv.DictReader((l.decode("utf-8", errors="replace") for l in f_csv), delimiter=";")
                                extraire_lignes_sitadel(lecteur, type_permis_label, est_demolition, annee_min, chantiers_sitadel, max_lignes_analysees)
            except Exception: pass
        logger.info(f"Sitadel : {len(chantiers_sitadel)} permis B2B retenus.")
        return chantiers_sitadel, "OK"
    except Exception as e:
        logger.error(f"Erreur SITADEL: {e}")
        return [], str(e)


# =============================================================================
# 5. DÉDOUBLONNAGE ET EXPORT
# =============================================================================

def dedoublonner_et_trier(chantiers: list[dict]) -> list[dict]:
    uniques = {ch["id_unique"]: ch for ch in chantiers}
    return sorted(uniques.values(), key=lambda x: (1 if x["statut_fiabilite"] == "Confirmé" else 0, x["score"], x["date_publication"]), reverse=True)

def exporter_csv(chantiers: list[dict]):
    colonnes = ["id_unique", "statut_fiabilite", "score", "raisons_score", "date_publication", "departement", "commune_adresse", "entreprise_cible", "role_entreprise", "engin_probable", "description_travaux", "accroche_telephonique", "source", "lien_officiel"]
    with open(FICHIER_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=colonnes, delimiter=";")
        writer.writeheader()
        writer.writerows([{k: v for k, v in c.items() if k in colonnes} for c in chantiers])


def exporter_webapp_mobile(chantiers: list[dict], etat_sources: dict):
    """Génère l'interface mobile avec le générateur PDF de devis intégré et les mentions légales exactes."""
    nb_confirmes = sum(1 for c in chantiers if c["statut_fiabilite"] == "Confirmé")
    date_jour = datetime.now().strftime("%d/%m/%Y à %H:%M")

    banniere_pannes = "".join([f'<div class="alerte-panne">⚠️ {html.escape(s)}</div>' for s in etat_sources.values() if s != "OK"])

    cartes_html = ""
    for ch in chantiers[:25]:
        est_confirme = ch["statut_fiabilite"] == "Confirmé"
        badge_classe = "badge-confirme" if est_confirme else "badge-verifier"
        badge_texte = "🟢 ATTRIBUÉ" if est_confirme else "🟠 À VÉRIFIER"
        lien_recherche_tel = f"https://www.google.com/search?q={quote(ch['entreprise_cible'] + ' téléphone BTP')}"

        cartes_html += f"""
        <div class="carte" data-statut="{ch['statut_fiabilite']}">
            <div class="carte-entete">
                <span class="badge {badge_classe}">{badge_texte}</span>
                <span class="score">Score : <strong>{ch['score']}</strong></span>
            </div>
            <h2 class="entreprise">{html.escape(ch['entreprise_cible'])}</h2>
            <div class="role">📍 <strong>{html.escape(ch['commune_adresse'])}</strong></div>

            <div class="actions">
                <a href="{lien_recherche_tel}" target="_blank" class="btn btn-appeler">📞 Chercher N°</a>
                <button class="btn btn-devis" onclick="ouvrirModal(this)"
                    data-client="{html.escape(ch['entreprise_cible'])}"
                    data-adresse="{html.escape(ch['commune_adresse'])}"
                    data-materiel="{html.escape(ch['engin_probable'])}"
                    data-travaux="{html.escape(ch['description_travaux'])}">
                    📝 Créer le devis
                </button>
            </div>

            <details class="details-bloc">
                <summary>Détails du chantier & Matériel</summary>
                <div class="contenu-details">
                    <p><strong>Travaux :</strong> {html.escape(ch['description_travaux'])}</p>
                    <p class="bloc-engin">🚜 <strong>Matériel :</strong> {html.escape(ch['engin_probable'])}</p>
                    <p class="raisons">💡 <em>Pourquoi : {html.escape(ch['raisons_score'])}</em></p>
                </div>
            </details>
        </div>
        """

    page_html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Radar I.T.L.T.P.</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
    <style>
        :root {{ --jaune-tp: #f59e0b; --fond: #0f172a; --carte: #1e293b; --texte: #f8fafc; --secondaire: #94a3b8; }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        body {{ background: var(--fond); color: var(--texte); padding: 12px; max-width: 600px; margin: 0 auto; }}
        header {{ margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #334155; }}
        h1 {{ font-size: 1.2rem; color: var(--jaune-tp); margin-bottom: 4px; }}
        .carte {{ background: var(--carte); border-radius: 12px; padding: 16px; margin-bottom: 12px; border: 1px solid rgba(255,255,255,0.05); }}
        .carte-entete {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; font-size: 0.8rem; }}
        .badge {{ padding: 4px 8px; border-radius: 6px; font-weight: 700; }}
        .badge-confirme {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
        .badge-verifier {{ background: rgba(249, 115, 22, 0.2); color: #fb923c; }}
        .entreprise {{ font-size: 1.15rem; color: #fff; margin-bottom: 6px; }}
        .role {{ font-size: 0.9rem; color: var(--secondaire); margin-bottom: 16px; }}
        .actions {{ display: flex; gap: 10px; margin-bottom: 16px; }}
        .btn {{ flex: 1; text-align: center; text-decoration: none; padding: 12px 8px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; cursor: pointer; border: none; }}
        .btn-appeler {{ background: #e2e8f0; color: #0f172a; }}
        .btn-devis {{ background: var(--jaune-tp); color: #451a03; }}
        details {{ background: rgba(255,255,255,0.03); border-radius: 8px; margin-bottom: 8px; }}
        summary {{ padding: 12px; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
        .contenu-details {{ padding: 0 12px 12px 12px; font-size: 0.85rem; color: #cbd5e1; }}
        .bloc-engin {{ color: #fde68a; margin: 8px 0; }}

        .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; justify-content: center; align-items: center; padding: 20px; }}
        .modal-content {{ background: var(--carte); padding: 20px; border-radius: 12px; width: 100%; max-width: 500px; max-height: 90vh; overflow-y: auto; position: relative; }}
        .fermer-modal {{ position: absolute; right: 15px; top: 15px; font-size: 1.5rem; color: #fff; cursor: pointer; }}
        .form-group {{ margin-bottom: 15px; }}
        .form-group label {{ display: block; font-size: 0.85rem; color: var(--secondaire); margin-bottom: 5px; }}
        .form-group input {{ width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 1rem; }}
        .info-client {{ background: rgba(255,255,255,0.05); padding: 10px; border-radius: 6px; font-size: 0.85rem; margin-bottom: 15px; }}
    </style>
</head>
<body>
    <header><h1>🚜 Radar I.T.L.T.P.</h1></header>
    {banniere_pannes}
    {cartes_html}

    <div id="modalDevis" class="modal">
        <div class="modal-content">
            <span class="fermer-modal" onclick="fermerModal()">×</span>
            <h2 style="margin-bottom: 15px; color: var(--jaune-tp);">Générer un Devis PDF</h2>

            <div class="info-client">
                <strong>Client :</strong> <span id="mod-client"></span><br>
                <strong>Adresse :</strong> <span id="mod-adresse"></span>
            </div>

            <div class="form-group">
                <label>Matériel prévu (modifiable) :</label>
                <input type="text" id="mod-materiel">
            </div>
            <div class="form-group">
                <label>Prix Journalier HT (Avec chauffeur) :</label>
                <input type="number" id="mod-prix-jour" placeholder="Ex: 850">
            </div>
            <div class="form-group">
                <label>Forfait Transfert Porte-char HT :</label>
                <input type="number" id="mod-prix-transport" placeholder="Ex: 350">
            </div>

            <button class="btn btn-devis" style="width: 100%; margin-top: 10px;" onclick="creerEtTelechargerPDF()">
                📄 1. Télécharger le devis (PDF)
            </button>
            <a id="btn-mail-final" href="#" class="btn btn-appeler" style="display: none; width: 100%; margin-top: 15px;">
                📧 2. Rédiger l'e-mail au client
            </a>
        </div>
    </div>

    <!-- Modèle PDF invisible avec mentions légales de la SAS I.T.L.T.P. -->
    <div id="modele-pdf" style="display: none; padding: 40px; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #000; background: #fff; width: 800px;">
        <div style="display: flex; justify-content: space-between; border-bottom: 3px solid #f59e0b; padding-bottom: 20px; margin-bottom: 30px;">
            <div>
                <h1 style="margin: 0; color: #f59e0b; font-size: 28px;">I.T.L.T.P.</h1>
                <p style="margin: 5px 0 0 0; font-weight: bold;">Location avec op&eacute;rateur de mat&eacute;riel de construction</p>
                <p style="margin: 2px 0;">14 rue Maryse Basti&eacute;, 91430 Igny</p>
                <p style="margin: 2px 0; color: #666; font-size: 12px;">SAS &bull; SIRET : 490 692 407 00029 &bull; N&deg; TVA : FR92 490 692 407</p>
            </div>
            <div style="text-align: right;">
                <h2 style="margin: 0; font-size: 24px; color: #333;">DEVIS</h2>
                <p style="margin: 5px 0 0 0;">Date : <span id="pdf-date"></span></p>
            </div>
        </div>

        <div style="margin-bottom: 40px; text-align: right;">
            <div style="display: inline-block; text-align: left; background: #f8f9fa; padding: 15px; border-radius: 5px; min-width: 250px;">
                <p style="margin: 0 0 5px 0; font-size: 12px; color: #666;">CLIENT :</p>
                <h3 style="margin: 0; font-size: 16px;" id="pdf-client"></h3>
                <p style="margin: 5px 0 0 0; font-size: 14px;" id="pdf-adresse"></p>
            </div>
        </div>

        <p style="font-weight: bold; margin-bottom: 15px;">Objet : Mise à disposition de matériel avec chauffeur</p>
        <p style="margin-bottom: 20px; font-size: 14px;">Référence chantier : <span id="pdf-travaux"></span></p>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 30px;">
            <thead>
                <tr style="background-color: #f59e0b; color: #fff;">
                    <th style="padding: 10px; text-align: left; border: 1px solid #ddd;">Désignation</th>
                    <th style="padding: 10px; text-align: right; border: 1px solid #ddd;">Prix Unitaire HT</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="padding: 12px; border: 1px solid #ddd;" id="pdf-materiel"></td>
                    <td style="padding: 12px; border: 1px solid #ddd; text-align: right;"><span id="pdf-val-jour"></span> €</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border: 1px solid #ddd;">Forfait transfert porte-char (Aller/Retour)</td>
                    <td style="padding: 12px; border: 1px solid #ddd; text-align: right;"><span id="pdf-val-trans"></span> €</td>
                </tr>
            </tbody>
        </table>

        <div style="display: flex; justify-content: flex-end;">
            <table style="width: 300px; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px; font-weight: bold;">Total HT :</td>
                    <td style="padding: 8px; text-align: right; font-weight: bold;"><span id="pdf-total-ht"></span> €</td>
                </tr>
                <tr>
                    <td style="padding: 8px; color: #666;">TVA (20 %) :</td>
                    <td style="padding: 8px; text-align: right; color: #666;"><span id="pdf-tva"></span> €</td>
                </tr>
                <tr style="border-top: 2px solid #000;">
                    <td style="padding: 10px; font-weight: bold; font-size: 18px;">Total TTC :</td>
                    <td style="padding: 10px; text-align: right; font-weight: bold; font-size: 18px;"><span id="pdf-total-ttc"></span> €</td>
                </tr>
            </table>
        </div>
        <div style="margin-top: 50px; font-size: 11px; color: #888; text-align: center; border-top: 1px solid #eee; padding-top: 15px;">
            Devis valable 30 jours.<br>
            En cas d'acceptation, merci de retourner ce devis sign&eacute; et dat&eacute; avec la mention "Bon pour accord".
        </div>
    </div>

    <script>
        let clientEnCours = "";
        let adresseEnCours = "";

        function ouvrirModal(btn) {{
            clientEnCours = btn.getAttribute('data-client');
            adresseEnCours = btn.getAttribute('data-adresse');
            document.getElementById('mod-client').innerText = clientEnCours;
            document.getElementById('mod-adresse').innerText = adresseEnCours;
            document.getElementById('mod-materiel').value = btn.getAttribute('data-materiel');
            document.getElementById('pdf-travaux').innerText = btn.getAttribute('data-travaux');

            document.getElementById('btn-mail-final').style.display = 'none';
            document.getElementById('modalDevis').style.display = 'flex';
        }}

        function fermerModal() {{
            document.getElementById('modalDevis').style.display = 'none';
        }}

        function creerEtTelechargerPDF() {{
            const pJour = parseFloat(document.getElementById('mod-prix-jour').value) || 0;
            const pTrans = parseFloat(document.getElementById('mod-prix-transport').value) || 0;
            const totalHT = pJour + pTrans;
            const tva = totalHT * 0.20;
            const totalTTC = totalHT + tva;

            document.getElementById('pdf-date').innerText = new Date().toLocaleDateString('fr-FR');
            document.getElementById('pdf-client').innerText = clientEnCours;
            document.getElementById('pdf-adresse').innerText = adresseEnCours;
            document.getElementById('pdf-materiel').innerText = "Location avec chauffeur : " + document.getElementById('mod-materiel').value;
            document.getElementById('pdf-val-jour').innerText = pJour.toFixed(2);
            document.getElementById('pdf-val-trans').innerText = pTrans.toFixed(2);
            document.getElementById('pdf-total-ht').innerText = totalHT.toFixed(2);
            document.getElementById('pdf-tva').innerText = tva.toFixed(2);
            document.getElementById('pdf-total-ttc').innerText = totalTTC.toFixed(2);

            const element = document.getElementById('modele-pdf');
            element.style.display = 'block';

            const opt = {{
                margin: 0,
                filename: 'Devis_ITLTP_' + clientEnCours.replace(/[^a-zA-Z0-9]/g, '_') + '.pdf',
                image: {{ type: 'jpeg', quality: 0.98 }},
                html2canvas: {{ scale: 2, useCORS: true }},
                jsPDF: {{ unit: 'mm', format: 'a4', orientation: 'portrait' }}
            }};

            html2pdf().set(opt).from(element).save().then(() => {{
                element.style.display = 'none';

                const sujet = encodeURIComponent("Devis de location I.T.L.T.P. - " + adresseEnCours);
                const corps = encodeURIComponent(
                    "Bonjour,\\n\\nVeuillez trouver en pièce jointe notre devis concernant votre chantier.\\n\\n" +
                    "⚠️ [N'oubliez pas d'insérer le PDF que vous venez de télécharger !]\\n\\n" +
                    "Cordialement,\\nI.T.L.T.P.\\n14 rue Maryse Bastié, 91430 Igny"
                );
                const btnMail = document.getElementById('btn-mail-final');
                btnMail.href = "mailto:?subject=" + sujet + "&body=" + corps;
                btnMail.style.display = 'block';
            }});
        }}
    </script>
</body>
</html>"""

    with open(FICHIER_HTML, "w", encoding="utf-8") as f:
        f.write(page_html)

def envoyer_email_rapport(fichier_html):
    api_key = os.environ.get("BREVO_API_KEY")
    destinataire = os.environ.get("EMAIL_RECEIVER") # ou l'adresse de ton oncle

    if not api_key or not destinataire:
        logger.info("Mode local : Clé API Brevo absente. Envoi ignoré.")
        return

    logger.info(f"Envoi du rapport par e-mail via l'API Brevo à {destinataire}...")

    # Lecture et encodage du fichier HTML en pièce jointe (Base64)
    with open(fichier_html, "rb") as f:
        contenu_html_bytes = f.read()
    
    import base64
    piece_jointe_b64 = base64.b64encode(contenu_html_bytes).decode("utf-8")

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }
    
    date_jour = datetime.now().strftime('%d/%m/%Y')
    payload = {
        "sender": {"name": "Radar I.T.L.T.P.", "email": "ne-pas-repondre@radarchantiers.fr"},
        "to": [{"email": destinataire}],
        "subject": f"🚜 Radar I.T.L.T.P. - Chantiers de la semaine ({date_jour})",
        "htmlContent": "<p>Bonjour,</p><p>Veuillez trouver ci-joint le radar des chantiers détectés cette semaine pour l'entreprise I.T.L.T.P.</p><p>Ouvrez le fichier HTML joint sur votre mobile pour consulter les opportunités et générer vos devis.</p><p>Bonne semaine !</p>",
        "attachment": [{
            "content": piece_jointe_b64,
            "name": f"Radar_ITLTP_{date_jour.replace('/', '-')}.html"
        }]
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code in [200, 201, 202]:
            logger.info("✅ E-mail envoyé avec succès via Brevo !")
        else:
            logger.error(f"❌ Erreur Brevo ({response.status_code}) : {response.text}")
    except Exception as e:
        logger.error(f"❌ Échec de l'envoi de l'e-mail : {e}")

def executer_autotests():
    res = qualifier_chantier("peinture", "91", "IGNY", True)
    assert not res["pertinent"]

if __name__ == "__main__":
    executer_autotests()
    ch_b, st_b = collecter_boamp()
    ch_s, st_s = collecter_sitadel()
    tous = dedoublonner_et_trier(ch_b + ch_s)
    exporter_webapp_mobile(tous, {"BOAMP": st_b, "SITADEL": st_s})
    envoyer_email_rapport(FICHIER_HTML)
    logger.info("Exécution terminée.")
