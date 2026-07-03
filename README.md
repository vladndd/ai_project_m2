# Agent Réglementaire — comment exécuter la solution

Ce dossier contient la solution concrète du projet *« Modélisation et structuration
automatique des données issues des réglementations dans le monde »*.

## Contenu du dossier

| Fichier | Rôle |
|---|---|
| `Rapport_Projet_Reglementations.docx` | Raisonnement complet : hypothèses testées, pistes abandonnées, méthodes essayées, évaluation, limites, questions pour la suite. |
| `Notebook_Extraction_Reglementations.ipynb` | Notebook Jupyter : code des 3 approches testées, résultats sur les échantillons Codex/UE/Inde/Thaïlande, graphique de comparaison. |
| `extraire_reglementation.py` | **L'outil exécutable** : script en ligne de commande qui transforme un PDF réglementaire en matrice structurée. |
| `data/pdf_samples/` | Les documents sources utilisés pour les tests (Codex, UE, Inde, Thaïlande). |
| `matrice_reglementaire_consolidee.csv` | Exemple de matrice consolidée produite par le notebook. |

## Prérequis

- Python 3.9 ou plus récent
- Poppler (fournit `pdftotext`, `pdffonts` et `pdftoppm`, utilisés pour lire les PDF)
  - macOS : `brew install poppler`
  - Ubuntu/Debian : `sudo apt-get install poppler-utils`
  - Windows : installer Poppler et ajouter le dossier `bin` au `PATH`
- Tesseract est optionnel : il sert seulement à tester l'OCR sur les PDF scannés
  - macOS : `brew install tesseract`
  - Ubuntu/Debian : `sudo apt-get install tesseract-ocr`
- Une clé API Anthropic, OpenAI **ou Gemini**, **uniquement si vous voulez activer l'extraction par LLM**
  (sans clé, l'outil fonctionne quand même en mode gratuit, voir plus bas)

## Installation reproductible avec environnement virtuel

### macOS / Linux
```bash
cd "Livrables"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Windows PowerShell
```powershell
cd Livrables
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Clé API LLM

Copier le modèle `.env.example` vers `.env`, puis remplir la clé du fournisseur choisi :

```bash
cp .env.example .env
```

Exemple pour Gemini dans `.env` :

```text
GEMINI_API_KEY=votre_cle_api
```

## Exécution

### Mode 1 — sans clé API (gratuit, immédiat)

N'utilise que les approches par règles (regex) et par analyse de la mise en page
(colonnes du tableau). Rapide, mais ne fonctionne bien que sur des documents
déjà structurés en tableau (ex. Codex, UE).

```bash
python extraire_reglementation.py "data/pdf_samples/codex_192_1995_additives.pdf" --pays "Codex" --no-llm
```

### Mode 2 — avec extraction par LLM (recommandé, généralise à tout pays/langue)

Avec **Claude** (Anthropic, par défaut) :
```bash
export ANTHROPIC_API_KEY="votre_cle_api"
python extraire_reglementation.py "data/pdf_samples/thailand_414_2020_contaminants.pdf" --pays "Thaïlande"
```

Avec **GPT** (OpenAI) :
```bash
export OPENAI_API_KEY="votre_cle_api"
python extraire_reglementation.py "data/pdf_samples/thailand_414_2020_contaminants.pdf" --pays "Thaïlande" --provider openai
```

Avec **Gemini** :
```bash
export GEMINI_API_KEY="votre_cle_api"
python extraire_reglementation.py "data/pdf_samples/thailand_414_2020_contaminants.pdf" --pays "Thaïlande" --provider gemini
```

### Traiter tout un dossier de documents pour un pays

```bash
python extraire_reglementation.py "data/pdf_samples" --pays "Nom du pays" --provider gemini
```

### Autres options utiles

```bash
python extraire_reglementation.py --help
```

- `--provider anthropic|openai|gemini` : choisir le fournisseur du LLM (défaut : `anthropic`)
- `-o resultat.csv` : choisir le nom du fichier de sortie
- `--model` : changer de modèle (défaut : `claude-sonnet-5` pour Anthropic, `gpt-4o` pour OpenAI, `gemini-3.5-flash` pour Gemini)
- `--chunk-size` : ajuster la taille des morceaux de texte envoyés au modèle
  (réduire si les réponses sont tronquées sur de gros tableaux)

## Résultat produit

Deux fichiers, `matrice_<pays>.csv` et `.json`, avec une ligne par règle extraite :
substance, catégorie d'aliment, type de valeur (minimum/maximum/plage/interdiction),
valeur, unité, conditions, indicateur d'ambiguïté, méthode d'extraction utilisée
(`regex` / `layout` / `llm_anthropic` / `llm_openai`), et le texte source exact pour vérification.

## Lancer le notebook

Après activation de l'environnement virtuel :

```bash
python -m notebook Notebook_Extraction_Reglementations.ipynb
```

Puis dans Jupyter :

```text
Kernel -> Restart Kernel and Run All Cells
```

Le notebook lit automatiquement `.env` si le fichier existe dans le dossier `Livrables`.

## En cas de PDF scanné (sans texte)

L'outil le détecte automatiquement et prévient qu'une étape d'OCR est nécessaire
avant extraction (voir section 6.2 du rapport pour le détail de ce cas, rencontré
sur un document thaïlandais de l'échantillon).

## Pour comprendre le raisonnement derrière l'outil

Le script est l'implémentation directe des 3 approches comparées dans le rapport
et le notebook (section 4 du rapport). Pour le contexte, les choix de modélisation
et l'évaluation chiffrée, se référer à `Rapport_Projet_Reglementations.docx`.
