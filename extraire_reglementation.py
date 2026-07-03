#!/usr/bin/env python3
"""
extraire_reglementation.py
============================

Outil en ligne de commande pour transformer un document réglementaire brut
(PDF ou texte) en une matrice structurée de règles (nutriments, additifs,
contaminants), selon le schéma défini dans le projet "Agent Réglementaire".

Ce script est la mise en oeuvre concrète et exécutable des 3 approches
comparées dans le notebook du projet :
  - A. règles/mots-clés (regex) sur les tableaux à colonnes fixes,
  - B. analyse structurelle de la mise en page (colonnes),
  - C. extraction par LLM (Anthropic Claude, OpenAI GPT ou Google Gemini),
    avec le pays en paramètre.

INSTALLATION (à faire une seule fois, sur ta machine)
-------------------------------------------------------
    pip install pdfplumber pandas
    pip install anthropic   # si tu utilises un modèle Claude (--provider anthropic, par défaut)
    pip install openai      # si tu utilises un modèle GPT (--provider openai)
    pip install google-genai # si tu utilises Gemini (--provider gemini)

Il faut aussi que `pdftotext` (Poppler) soit installé sur la machine :
  - macOS   : brew install poppler
  - Ubuntu  : sudo apt-get install poppler-utils
  - Windows : installer Poppler et l'ajouter au PATH

CLÉ API (nécessaire seulement pour l'approche C, LLM)
-------------------------------------------------------
    export ANTHROPIC_API_KEY="sk-ant-..."     # pour --provider anthropic (défaut)
    export OPENAI_API_KEY="sk-..."            # pour --provider openai
    export GEMINI_API_KEY="..."               # pour --provider gemini

USAGE
-------------------------------------------------------
    # Un seul document (avec Claude, par défaut)
    python extraire_reglementation.py mon_fichier.pdf --pays "Brésil"

    # Avec OpenAI/GPT à la place de Claude
    python extraire_reglementation.py mon_fichier.pdf --pays "Brésil" --provider openai

    # Avec Gemini
    python extraire_reglementation.py mon_fichier.pdf --pays "Brésil" --provider gemini

    # Un dossier entier de documents pour un même pays
    python extraire_reglementation.py ./dossier_bresil/ --pays "Brésil" --provider gemini

    # Sans appel LLM (seulement les approches A et B, gratuites, sans clé API)
    python extraire_reglementation.py mon_fichier.pdf --pays "Brésil" --no-llm

    # Choisir le fichier de sortie
    python extraire_reglementation.py mon_fichier.pdf --pays "Chine" -o resultat_chine.csv

Le résultat est un fichier CSV (et un .json détaillé équivalent) contenant une
ligne par règle extraite, avec sa méthode d'extraction et le texte source
d'origine pour audit.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List

# ---------------------------------------------------------------------------
# 0. Schéma cible (voir section 3 du rapport / notebook du projet)
# ---------------------------------------------------------------------------

@dataclass
class RegulatoryRule:
    country: str
    source_document: str
    topic: Optional[str] = None
    substance_name: str = ""
    substance_code: Optional[str] = None
    substance_category: Optional[str] = None
    food_category_label: Optional[str] = None
    conditions: str = ""                 # conditions jointes par " ; " (aplati pour le CSV)
    value_type: str = "maximum"          # minimum | maximum | range | interdiction | declaration_obligatoire
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    unit: Optional[str] = None
    basis: Optional[str] = None
    is_ambiguous: bool = False
    ambiguity_note: Optional[str] = None
    footnote_refs: Optional[str] = None
    raw_text_span: str = ""
    extraction_method: str = "manuel"    # regex | layout | llm
    chunk_index: Optional[int] = None


# ---------------------------------------------------------------------------
# 1. Extraction du texte brut (PDF -> texte, mise en page préservée)
# ---------------------------------------------------------------------------

def extract_text(path):
    """Retourne le texte brut d'un fichier .pdf ou .txt."""
    if path.lower().endswith(".txt"):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    if path.lower().endswith(".pdf"):
        # 1) Tentative avec pdftotext -layout (le plus fidèle pour les tableaux)
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", path, "-"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # 2) Repli sur pdfplumber si pdftotext est indisponible
        try:
            import pdfplumber
            text_parts = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text_parts.append(page.extract_text() or "")
            return "\n".join(text_parts)
        except ImportError:
            print("! Ni pdftotext ni pdfplumber ne sont disponibles : "
                  "installe l'un des deux (voir l'en-tête du script).", file=sys.stderr)
            return ""

    raise ValueError(f"Format non pris en charge : {path} (attendu : .pdf ou .txt)")


def has_text_layer(pdf_path):
    """Diagnostic : le PDF a-t-il une couche texte, ou est-ce un scan (image) ?"""
    try:
        result = subprocess.run(["pdffonts", pdf_path], capture_output=True, text=True, timeout=30)
        lines = [l for l in result.stdout.splitlines() if l and not l.startswith(("name", "---"))]
        return len(lines) > 0
    except FileNotFoundError:
        return None  # pdffonts non disponible, on ne peut pas trancher


# ---------------------------------------------------------------------------
# 1bis. Suivi du "contexte" (nom de la substance en cours) pour les approches
#       A et B, qui autrement ne savent traiter qu'une ligne à la fois.
#       Heuristique : dans les tableaux Codex/GSFA, chaque section commence
#       par une ligne de titre en MAJUSCULES (ex. "ACESULFAME POTASSIUM").
#       Imparfait par construction (peut se tromper sur d'autres lignes tout
#       en majuscules, ex. en-têtes de page) — documenté comme limite connue,
#       cf. section 4.1/4.2 du rapport.
# ---------------------------------------------------------------------------

_HEADING_EXCLUDE = ("CODEX STAN", "TABLE ONE", "TABLE TWO", "GENERAL STANDARD",
                    "FOODCATNO", "NOTES TO", "ADDITIVES PERMITTED")

def build_heading_map(lines):
    headings = [None] * len(lines)
    current = None
    for i, line in enumerate(lines):
        s = line.strip()
        if (s and s == s.upper() and re.search(r"[A-Z]{3,}", s) and len(s) <= 80
                and not s[0].isdigit() and "mg/kg" not in s
                and not any(s.startswith(x) for x in _HEADING_EXCLUDE)):
            current = s.title()
        headings[i] = current
    return headings


# ---------------------------------------------------------------------------
# 2. Approche A — règles / mots-clés (regex sur tableaux à colonnes fixes)
#    Rapide, gratuite, mais seulement utile si le document a un tableau au
#    format "code catégorie / libellé / valeur unité / notes / année"
#    (style Codex / GSFA). Voir section 4.1 du rapport pour ses limites.
# ---------------------------------------------------------------------------

ROW_PATTERN = re.compile(
    r"^(?P<cat_code>\d{2}(?:\.\d+)+|\d{2}\.\d)\s{2,}(?P<cat_name>.+?)\s{2,}"
    r"(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>mg/kg|g/kg|mg/L|%)\s+"
    r"(?P<notes>[\w,&\s]+?)\s{2,}(?P<year>\d{4})\s*$"
)

def extract_regex(text, country, source_document, substance_hint=""):
    lines = text.split("\n")
    headings = build_heading_map(lines)
    rules = []
    for i, line in enumerate(lines):
        m = ROW_PATTERN.match(line)
        if not m:
            continue
        d = m.groupdict()
        rules.append(RegulatoryRule(
            country=country, source_document=source_document,
            substance_name=substance_hint or headings[i] or "(non identifié, voir raw_text_span)",
            food_category_label=d["cat_name"].strip(),
            value_type="maximum",
            value_max=float(d["value"].replace(",", "")),
            unit=d["unit"],
            footnote_refs=d["notes"].strip(),
            raw_text_span=m.group(0).strip(),
            extraction_method="regex",
        ))
    return rules


# ---------------------------------------------------------------------------
# 3. Approche B — analyse structurelle de la mise en page (colonnes)
#    Indépendante de la langue, mais nécessite un texte déjà tabulaire et
#    calibré (voir section 4.2 du rapport pour ses limites : lignes de
#    continuation perdues, calibrage par document).
# ---------------------------------------------------------------------------

def split_columns(line, min_gap=2):
    return [c.strip() for c in re.split(rf"\s{{{min_gap},}}", line.strip()) if c.strip()]

def looks_numeric(token):
    """Un nombre plausible (valeur/seuil) : pas un code de catégorie du type
    '04.2.2.4' (plusieurs points), ni un simple numéro d'article."""
    return bool(re.match(r"^\d+([.,]\d+)?$", token))

def extract_layout(text, country, source_document, expect_cols=3, substance_hint=""):
    lines = text.split("\n")
    headings = build_heading_map(lines)
    rules = []
    for i, line in enumerate(lines):
        cols = split_columns(line)
        if len(cols) >= expect_cols and any(looks_numeric(c) for c in cols):
            numeric_idx = next(j for j, c in enumerate(cols) if looks_numeric(c))
            label = cols[0] if numeric_idx != 0 else (cols[1] if len(cols) > 1 else "")
            rules.append(RegulatoryRule(
                country=country, source_document=source_document,
                substance_name=substance_hint or headings[i] or "(non identifié, voir raw_text_span)",
                food_category_label=label,
                value_type="maximum",
                value_max=float(cols[numeric_idx].replace(",", ".")),
                raw_text_span=line.strip(),
                extraction_method="layout",
            ))
    return rules


# ---------------------------------------------------------------------------
# 4. Approche C — extraction par LLM (Claude), pays en paramètre du prompt
#    C'est l'approche recommandée dans le rapport pour généraliser à un
#    nouveau pays / une nouvelle langue sans travail d'ingénierie répété.
# ---------------------------------------------------------------------------

def load_env_file():
    """Charge un fichier .env local sans dépendance externe.

    Priorité : variables déjà présentes dans l'environnement, puis .env du
    dossier courant, puis .env à côté de ce script.
    """
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


EXTRACTION_PROMPT = """Tu es un assistant d'extraction réglementaire. On te donne un
extrait de texte issu d'une réglementation alimentaire (pays ou organisme : {country}).

Identifie toutes les règles concernant des nutriments, additifs ou contaminants dans cet
extrait, et retourne UNIQUEMENT un tableau JSON (pas de texte autour, pas de balises
markdown) où chaque élément suit exactement ce schéma :

[{{
  "substance_name": str,
  "substance_code": str ou null,
  "food_category_label": str ou null,
  "value_type": "minimum" | "maximum" | "range" | "interdiction" | "declaration_obligatoire",
  "value_min": nombre ou null,
  "value_max": nombre ou null,
  "unit": str ou null,
  "conditions": [str],
  "is_ambiguous": true ou false,
  "ambiguity_note": str ou null,
  "raw_text_span": str
}}]

Règles :
- Si l'extrait indique une interdiction ("ne doit pas être détecté", "interdit"), utilise
  value_type = "interdiction" et laisse value_min/value_max à null (n'invente pas un 0).
- Si une condition ou exception module la règle générale, décris-la dans "conditions"
  plutôt que de créer une règle non reliée à la substance.
- Si la formulation est ambiguë ou nécessite une interprétation juridique, mets
  is_ambiguous = true et explique pourquoi dans ambiguity_note, sans trancher toi-même.
- S'il n'y a aucune règle exploitable dans l'extrait, retourne [].

Extrait à analyser :
---
{text_excerpt}
---
"""


def chunk_text(text, chunk_size=6000, overlap=200):
    """Découpe un texte long en morceaux qui tiennent dans un appel LLM,
    avec un léger chevauchement pour ne pas couper une règle en deux."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def try_parse_json_array(raw):
    """Tente de parser le JSON renvoyé par le modèle. Si la réponse a été
    coupée avant la fin (limite de tokens atteinte en plein milieu d'un
    tableau), on répare en revenant au dernier objet complet et en refermant
    le tableau à cet endroit, plutôt que de tout jeter."""
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass
    positions = {m.end() for m in re.finditer(r"\}\s*,", raw)}
    positions |= {m.end() for m in re.finditer(r"\}\s*\]", raw)}
    for pos in sorted(positions, reverse=True):
        candidate = raw[:pos].rstrip().rstrip(",") + "]"
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            continue
    return None, True


PROVIDER_DEFAULTS = {
    "anthropic": {"env_var": "ANTHROPIC_API_KEY", "default_model": "claude-sonnet-5", "package": "anthropic"},
    "openai": {"env_var": "OPENAI_API_KEY", "default_model": "gpt-4o", "package": "openai"},
    "gemini": {"env_var": "GEMINI_API_KEY", "default_model": "gemini-3.5-flash", "package": "google-genai"},
}


def get_llm_client(provider):
    """Instancie le client du fournisseur choisi, en réutilisant la clé API
    depuis la variable d'environnement correspondante."""
    info = PROVIDER_DEFAULTS[provider]
    api_key = os.environ.get(info["env_var"])
    if not api_key:
        print(f"! Variable d'environnement {info['env_var']} absente. "
              f"L'approche LLM est ignorée (utilise --no-llm pour supprimer ce message).",
              file=sys.stderr)
        return None

    try:
        if provider == "anthropic":
            import anthropic
        elif provider == "openai":
            import openai
        elif provider == "gemini":
            from google import genai
    except ImportError:
        print(f"! Le paquet '{info['package']}' n'est pas installé. "
              f"Lance : pip install {info['package']}", file=sys.stderr)
        return None

    try:
        if provider == "anthropic":
            return anthropic.Anthropic(api_key=api_key)
        elif provider == "openai":
            return openai.OpenAI(api_key=api_key)
        elif provider == "gemini":
            return genai.Client(api_key=api_key)
    except Exception as e:
        print(f"! Impossible d'initialiser le client {provider} : {e}", file=sys.stderr)
        return None


def call_llm_extraction(provider, client, model, country, text_excerpt, max_retries=2):
    prompt = EXTRACTION_PROMPT.format(country=country, text_excerpt=text_excerpt)
    for attempt in range(max_retries + 1):
        try:
            if provider == "anthropic":
                response = client.messages.create(
                    model=model,
                    max_tokens=8000,
                    messages=[{"role": "user", "content": prompt}],
                )
                # Avec certains modèles, response.content peut contenir un bloc de
                # "réflexion" (ThinkingBlock) avant le bloc de texte proprement dit :
                # on ne garde que les blocs de type "text".
                text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
                raw = "\n".join(text_blocks).strip()
            elif provider == "openai":
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                raw = (response.choices[0].message.content or "").strip()
            elif provider == "gemini":
                response = client.interactions.create(
                    model=model,
                    input=prompt,
                    generation_config={"temperature": 0},
                )
                raw = (getattr(response, "output_text", "") or "").strip()
            else:
                raise ValueError(f"Fournisseur inconnu : {provider}")

            # tolère un éventuel encadrement ```json ... ```
            raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            result, was_truncated = try_parse_json_array(raw)
            if result is not None:
                if was_truncated:
                    print(f"  ! Réponse tronquée par le modèle, {len(result)} règle(s) "
                          f"récupérée(s) avant la coupure (essaie --chunk-size plus petit "
                          f"pour éviter ça).", file=sys.stderr)
                return result
            print(f"  ! Réponse non-JSON et irréparable (tentative {attempt+1})", file=sys.stderr)
        except Exception as e:
            print(f"  ! Erreur d'appel API (tentative {attempt+1}) : {e}", file=sys.stderr)
            time.sleep(2)
    return []


def extract_llm(text, country, source_document, provider="anthropic", model=None, chunk_size=3500):
    model = model or PROVIDER_DEFAULTS[provider]["default_model"]
    client = get_llm_client(provider)
    if client is None:
        return []

    rules = []
    chunks = chunk_text(text, chunk_size=chunk_size)
    print(f"  -> {len(chunks)} morceau(x) à envoyer au modèle {model} ({provider})...")
    for i, chunk in enumerate(chunks):
        print(f"     chunk {i+1}/{len(chunks)}...")
        items = call_llm_extraction(provider, client, model, country, chunk)
        for item in items:
            rules.append(RegulatoryRule(
                country=country, source_document=source_document,
                substance_name=item.get("substance_name", ""),
                substance_code=item.get("substance_code"),
                food_category_label=item.get("food_category_label"),
                conditions=" ; ".join(item.get("conditions") or []),
                value_type=item.get("value_type", "maximum"),
                value_min=item.get("value_min"),
                value_max=item.get("value_max"),
                unit=item.get("unit"),
                is_ambiguous=bool(item.get("is_ambiguous", False)),
                ambiguity_note=item.get("ambiguity_note"),
                raw_text_span=item.get("raw_text_span", ""),
                extraction_method=f"llm_{provider}",
                chunk_index=i,
            ))
    return rules


# ---------------------------------------------------------------------------
# 5. Orchestration
# ---------------------------------------------------------------------------

def process_file(path, country, use_llm, provider, model, chunk_size):
    print(f"\n=== {os.path.basename(path)} ===")
    if path.lower().endswith(".pdf"):
        layer = has_text_layer(path)
        if layer is False:
            print("! Ce PDF semble scanné (pas de couche texte détectée par pdffonts). "
                  "Une étape d'OCR est nécessaire avant extraction (voir section 6.2 du rapport).")

    text = extract_text(path)
    if not text.strip():
        print("! Aucun texte exploitable extrait de ce fichier.")
        return []

    all_rules = []
    all_rules += extract_regex(text, country, os.path.basename(path))
    all_rules += extract_layout(text, country, os.path.basename(path))
    if use_llm:
        all_rules += extract_llm(text, country, os.path.basename(path),
                                  provider=provider, model=model, chunk_size=chunk_size)

    n_llm = sum(1 for r in all_rules if r.extraction_method.startswith("llm"))
    print(f"  Règles trouvées : regex={sum(1 for r in all_rules if r.extraction_method=='regex')}, "
          f"layout={sum(1 for r in all_rules if r.extraction_method=='layout')}, "
          f"llm={n_llm}")
    return all_rules


def main():
    load_env_file()

    parser = argparse.ArgumentParser(
        description="Extrait une matrice de règles réglementaires à partir d'un PDF/texte.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="Fichier .pdf/.txt, ou dossier contenant plusieurs documents.")
    parser.add_argument("--pays", "--country", dest="country", required=True,
                         help="Nom du pays/organisme (ex. 'Brésil', 'Chine', 'Codex').")
    parser.add_argument("-o", "--output", default=None,
                         help="Chemin du fichier CSV de sortie (défaut : matrice_<pays>.csv).")
    parser.add_argument("--no-llm", action="store_true",
                         help="Désactive l'approche C (LLM) — seules les approches A et B tournent (gratuit, sans clé API).")
    parser.add_argument("--provider", choices=["anthropic", "openai", "gemini"], default="anthropic",
                         help="Fournisseur du LLM à utiliser (défaut : anthropic). "
                              "Utilise 'openai' avec OPENAI_API_KEY ou 'gemini' avec GEMINI_API_KEY.")
    parser.add_argument("--model", default=None,
                         help="Modèle à utiliser pour l'extraction LLM. Défaut selon le fournisseur : "
                              "claude-sonnet-5 (anthropic), gpt-4o (openai) ou gemini-3.5-flash (gemini).")
    parser.add_argument("--chunk-size", type=int, default=3500,
                         help="Taille (en caractères) des morceaux envoyés au LLM (défaut : 3500).")
    args = parser.parse_args()

    if os.path.isdir(args.input):
        files = sorted(glob.glob(os.path.join(args.input, "*.pdf")) +
                        glob.glob(os.path.join(args.input, "*.txt")))
        if not files:
            print(f"Aucun fichier .pdf/.txt trouvé dans {args.input}", file=sys.stderr)
            sys.exit(1)
    else:
        files = [args.input]

    all_rules = []
    for f in files:
        all_rules += process_file(f, args.country, use_llm=not args.no_llm,
                                   provider=args.provider, model=args.model,
                                   chunk_size=args.chunk_size)

    if not all_rules:
        print("\nAucune règle extraite. Vérifie le fichier d'entrée, ou active --no-llm différemment.")
        sys.exit(0)

    out_csv = args.output or f"matrice_{re.sub(r'[^a-zA-Z0-9]+', '_', args.country)}.csv"
    out_json = os.path.splitext(out_csv)[0] + ".json"

    try:
        import pandas as pd
        df = pd.DataFrame([asdict(r) for r in all_rules])
        df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    except ImportError:
        # repli sans pandas : écriture CSV manuelle
        import csv
        fieldnames = list(asdict(all_rules[0]).keys())
        with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in all_rules:
                writer.writerow(asdict(r))

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in all_rules], f, ensure_ascii=False, indent=2)

    print(f"\n{len(all_rules)} règles extraites au total.")
    print(f"-> {out_csv}")
    print(f"-> {out_json}")


if __name__ == "__main__":
    main()
