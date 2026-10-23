# resume-ats-parser

Tailors your LaTeX resume to a job description with an open-weight LLM. Run it locally or through a manually triggered GitHub Actions workflow. Output: a one-page, ATS-friendly `.tex` plus a report of JD gaps.

```
resume-ats-parser/
├── .github/workflows/tailor-resume.yml   # workflow_dispatch pipeline
├── data/profile.json                     # your LinkedIn facts (fill in)
├── templates/base_resume.tex             # your base template (replace)
├── prompts/system.md                     # rules for the model
├── scripts/tailor.py                     # LLM call, validation, page check
├── jobs/                                 # JD text files
├── output/                               # results (git-ignored)
├── run_local.sh                          # local runner
├── .env.example                          # local LLM settings
└── requirements.txt
```

## Local test (open source, free)
1. Install Ollama and pull a model: `ollama pull llama3.1:8b` (a bigger model gives better results).
2. `cp .env.example .env` (defaults point to Ollama).
3. Replace `templates/base_resume.tex` with your real template and fill `data/profile.json`.
4. Save a JD in `jobs/my_jd.txt`, then run `./run_local.sh jobs/my_jd.txt schneider-medallia`.
5. Open `output/schneider-medallia.tex` and the `_report.md`. If `pdflatex` is installed, the script checks the page count and asks the model to trim until it is one page (max 2 retries).

To check the configured LLM endpoint and model before tailoring a resume, run:

```bash
.venv/bin/python scripts/check_llm.py
```

## GitHub Actions
Push to GitHub, then Actions, Tailor resume, Run workflow. It uses `GITHUB_TOKEN` with GitHub Models (free tier, open-weight models). Check your account's availability and rate limits.

## Guardrails
The model may only use facts from `profile.json` and the template, and must keep metrics unchanged. Skills in `skills_exposure` stay "hands-on exposure". JD requirements you don't meet appear in the report as gaps and are not added to the resume. Always read the output before applying.

## Run for a job description (Ollama)
Save the job description in `jobs/`, then run:

```bash
./run_local.sh jobs/sample_jd.txt electrolux-consumer-platform
```

Output in `output/`:
- `<slug>.tex` and `<slug>.pdf` (compiled once, for checking). The `.pdf` is made by pdflatex.
- `<slug>_report.md`: gaps, keywords, notes, and every problem the script fixed in the model's reply.
- `<slug>_llm_raw.txt`: the model's raw reply, for debugging.

To rerun without Ollama, save a reply as JSON and set `LLM_REPLY_FILE`:

```bash
LLM_REPLY_FILE=output/electrolux-consumer-platform_reply.json JD_FILE=jobs/sample_jd.txt JOB_SLUG=electrolux-consumer-platform .venv/bin/python scripts/tailor.py
```
