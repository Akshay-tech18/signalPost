#!/bin/bash
set -e

# Load environment variables safely from .env if present
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      \#*|"") continue ;;
      *) export "$line" ;;
    esac
  done < .env
fi

INPUT_FILE="entry-companies.jsonl"
BULK_FILE="brreg-enheter.csv.gz"
PROFILES_REGISTRY="out/entry-profiles-registry.jsonl"
PROFILES_TAVILY="out/entry-profiles-tavily.jsonl"
PROFILES_FINAL="out/entry-profiles-final.jsonl"
ENVELOPES_FINAL="out/entry-envelopes.jsonl"
RUN_ID="signalpost-submission-001"

echo "=========================================================="
echo "🚀 STARTING SIGNALPOST EVALUATION PIPELINE (1000 orgs)"
echo "=========================================================="

# Pre-flight environment and input checks
echo "[0/5] Running pre-flight checks..."
if [ ! -f "$INPUT_FILE" ]; then
  echo "❌ Error: Input file '$INPUT_FILE' not found."
  echo "   Run select_entry_batch.py or fix_entry_batch.py first."
  exit 1
fi

if [ ! -f "$BULK_FILE" ]; then
  if [ -f "brreg-enheter.csv" ]; then
    BULK_FILE="brreg-enheter.csv"
  else
    echo "❌ Error: Bulk registry file '$BULK_FILE' not found."
    echo "   Download it with: curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv.gz"
    exit 1
  fi
fi

if [ -z "$TAVILY_API_KEY" ]; then
  echo "⚠️ Warning: TAVILY_API_KEY is not set in environment or .env."
  echo "   Tavily web discovery (step 2) will fail without an API key."
fi

if [ -z "$GROQ_API_KEY" ]; then
  echo "⚠️ Warning: GROQ_API_KEY is not set in environment or .env."
  echo "   Groq LLM fact extraction (step 3) will fail without an API key."
fi

echo "✅ Pre-flight checks passed (Inputs: $INPUT_FILE, Bulk: $BULK_FILE)"

# Export PYTHONPATH to use the installed virtualenv packages and src
export PYTHONPATH=".venv/lib/python3.14/site-packages:src:${PYTHONPATH:-}"
PYTHON_CMD="python3"

# 1. Base Registry Extraction
echo -e "\n[1/5] Extracting Official Registry Data..."
$PYTHON_CMD scripts/run_competition_batch.py \
  --organisations "$INPUT_FILE" \
  --bulk "$BULK_FILE" \
  --output out/temp-envelopes.jsonl \
  --profiles-output "$PROFILES_REGISTRY" \
  --report out/entry-registry-report.json \
  --run-id "$RUN_ID" \
  --expected-count 1000 \
  --workers 16 \
  --resume

# 2. Tavily Web Discovery
echo -e "\n[2/5] Discovering Missing Websites via Tavily API..."
$PYTHON_CMD scripts/run_tavily_discovery.py \
  --input "$PROFILES_REGISTRY" \
  --output "$PROFILES_TAVILY" \
  --report out/entry-tavily-report.json \
  --promote-verified \
  --limit 1000 \
  --count 10 \
  --resume

# 3. Groq LLM Fact Extraction
echo -e "\n[3/5] Extracting Jobs and People via Groq..."
$PYTHON_CMD scripts/run_groq_extractor.py \
  --input "$PROFILES_TAVILY" \
  --output "$PROFILES_FINAL" \
  --report out/entry-groq-report.json \
  --model qwen/qwen3.8-27b \
  --resume

# 4. Verified Observation Ledger (55-Point Rubric)
echo -e "\n[4/5] Building Verified External Observation Ledger & Running Audit..."
$PYTHON_CMD scripts/build_all_observations.py \
  --profiles "$PROFILES_FINAL" \
  --output out/observations.jsonl \
  --labels-output out/observation-audit-labels.jsonl \
  --report out/observation-build-report.json

$PYTHON_CMD scripts/evaluate_external_footprint.py \
  --profiles "$PROFILES_FINAL" \
  --observations out/observations.jsonl \
  --labels out/observation-audit-labels.jsonl \
  --output out/external-footprint-report.json \
  --minimum-audit 100

# 5. Packaging and Competition Scoring
echo -e "\n[5/5] Packaging Envelopes and Calculating Official Competition Score..."
$PYTHON_CMD scripts/package_submission.py \
  --profiles "$PROFILES_FINAL" \
  --output "$ENVELOPES_FINAL" \
  --run-id "$RUN_ID" \
  --validate

$PYTHON_CMD scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-report.json

$PYTHON_CMD scripts/score_competition_v3.py \
  --profiles "$PROFILES_FINAL" \
  --external-report out/external-footprint-report.json \
  --batch-report out/entry-registry-report.json \
  --resume-report out/entry-registry-report.json \
  --refresh-report out/refresh-report.json \
  --research-report out/research-report.json \
  --ux-report out/ux-report.json \
  --output out/competition-score-report.json

echo -e "\n✅ PIPELINE & AUDIT EVALUATION COMPLETE!"
echo "Final submission envelope: $ENVELOPES_FINAL"
echo "External observation ledger: out/observations.jsonl"
echo "Competition audit score: out/competition-score-report.json"
