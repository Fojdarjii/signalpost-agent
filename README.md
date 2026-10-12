# Signalpost

Signalpost collects evidence-backed Norwegian company profiles from the official
[Brønnøysundregistrene Enhetsregisteret API](https://data.brreg.no/enhetsregisteret/).
It never invents missing facts: unsuccessful lookups are emitted as an error record,
and every successful field group carries its source URL and retrieval timestamp.

## Run

Use Python 3.10+:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m signalpost.cli companies.csv --output profiles.jsonl --workers 8
# Or use the budgeted async runner:
python agent.py --input orgnrs.txt --output profiles.jsonl --budget 1900
```

`companies.csv` may have a header (`organization_number`) or simply one 9-digit
organization number per line. The output is JSON Lines so 1,000+ records can be
streamed and inspected without holding the full batch in memory.

## Data and cost

The default collector uses no paid AI API and therefore has $0 AI-token cost.
Network/API costs are also $0 for the public registry endpoint, subject to its
terms and rate limits. The `roles` endpoint includes registered people and roles
when the registry publishes them; it is not a guarantee of beneficial ownership.
Annual revenue is not silently inferred because it requires a separate, year-
specific financial source. Add such a source only with its own provenance and
license/terms review.

## Quality guarantees

- Input must contain exactly 9 digits after whitespace/punctuation normalization.
- The returned organization number is checked against the requested number.
- HTTP 404 and other failures are written as explicit errors, never as profiles.
- Each profile contains official source URLs and UTC retrieval time.

Run the smoke test with:

```powershell
python -m unittest discover -s tests -v
```