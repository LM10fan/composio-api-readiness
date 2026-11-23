# Composio API Readiness Research Agent

This is a partially completed, resumable research run: 70 of 100 apps. The case-study page labels coverage and verification limits. It excludes the assignment reference snapshot.

## Run locally
1. Create a virtual environment and install `requirements.txt`.
2. Set `TAVILY_API_KEY` and either `GROQ_API_KEY` or `OPENROUTER_API_KEY`.
3. Run: `python agent/research_free_multi.py --provider groq --workers 1` or `--provider openrouter --workers 1`.
4. The runner writes per-app checkpoints under `data/live_research/` and resumes. Inspect `data/live_coverage.json` and `data/live_raw.json`.
5. Open `docs/submission.html` in a browser.

## Limits
This run used Tavily for search/evidence and Groq free inference for 37 rows plus OpenRouter free-model inference for 33 rows. Groq’s free daily token quota and OpenRouter’s free-model request quota were exhausted; some model responses were null or malformed. No Grok API or OpenAI API was used. No paid fallback is enabled. Tavily has separate plan limits. Free quotas and model availability change. URL matching confirms cited URLs appeared among retrieved pages; it does not validate claims. The 70-row output received a 10-row risk-focused manual spot-check, not full human validation. Do not report it as a statistical accuracy estimate.

`data/live_raw.json` and `data/live_coverage.json` are the actual uploaded outputs. `data/apps.json` is the assignment list. No reference snapshot or reference page is included.
