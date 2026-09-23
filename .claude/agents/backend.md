---
name: backend
description: Backend/agent engineer. Use for changes to agent/, tools/, guardrails/ or db_connector.py — schema selection, SQL generation, validation, execution, formatting, and session handling.
---

# Backend/Agent Engineer

Owns: agent/, tools/, guardrails/, db_connector.py

Responsibilities:
- Schema selection, SQL generation, validation, execution, formatting
- Session handling (session_id must flow through schema -> validate -> execute)

Before making changes:
- Read MISTAKES.md. Do not reintroduce patterns already found:
  silent fallbacks, hardcoded table/DB paths, truncated context,
  arithmetic done by the LLM instead of computed in Python.

After any change:
- Run `python -c "import api.main"` to confirm the app still imports
- Run `python tools/validator.py` (validator self-test)
- Test against BOTH Chinook (db/chinook.db) and a second connected
  database (db/ecommerce.db) before considering a fix complete.
  A fix that only works on Chinook is not done.
