set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || { echo "Create .env first: cp .env.example .env"; exit 1; }

export JD_FILE="${1:?Usage: ./run_local.sh jobs/jd.txt [slug] [instructions]}"
export JOB_SLUG="${2:-tailored}"
export EXTRA_INSTRUCTIONS="${3:-}"
.venv/bin/python scripts/tailor.py
