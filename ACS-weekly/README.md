# ACS & Secondary Prevention Weekly

Automated weekly literature surveillance for acute coronary syndrome, cardiac rehabilitation, and secondary cardiovascular prevention.

## What it does

1. Searches PubMed for the previous 7 days.
2. Keeps only journals listed in `config/q1_journals.txt`.
3. Uses the OpenAI API to classify and summarize eligible papers.
4. Generates a static HTML report in `docs/`.
5. Publishes via GitHub Pages.
6. Optionally sends an email through Gmail SMTP.

## GitHub Pages

Settings → Pages → Deploy from a branch → `main` → `/docs`.

Expected site:
`https://CARDIOLOGY-risk.github.io/ACS-weekly/`

## GitHub Actions secrets

Settings → Secrets and variables → Actions.

Required:
- `OPENAI_API_KEY`
- `NCBI_EMAIL`

For email:
- `SMTP_USER`
- `SMTP_APP_PASSWORD` (Google App Password; not your normal password)
- `REPORT_RECIPIENT`

Recommended:
- `SITE_URL` = `https://CARDIOLOGY-risk.github.io/ACS-weekly/`
- `OPENAI_MODEL` = model you want to use; if omitted, script defaults to `gpt-5-mini`.

## Q1 filter

PubMed does not provide journal quartiles. The file `config/q1_journals.txt` is therefore an explicit allow-list. Verify it periodically against your chosen current JCR source and edit it as needed.

## First test

Actions → Weekly ACS literature report → Run workflow.

The workflow is scheduled for Mondays at 07:10 UTC and can also be run manually.
