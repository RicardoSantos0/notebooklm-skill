# NotebookLM Canonical Invocation Template
# Used by all MAS agents that query NotebookLM.
# Do not modify without updating all agent definitions that reference it.
# Version: 1.0 | Date: 2026-04-11

## When to Query NotebookLM

Query NotebookLM when you need grounded, citation-backed answers on:
- Agent design, orchestration, and multi-agent architecture
- Governance, evaluation, and project management frameworks
- Database design, storage, and query patterns
- ML/DL concepts and implementation patterns
- Any decision where hallucination risk is high or citations are required

Do NOT query for: live system state, project-specific data, or anything already
in shared state or local files.

## Invocation Pattern (for agents with execute access)

```bash
cd C:/Users/ricar/Documents/claude-config/skills/notebooklm
PYTHONIOENCODING=utf-8 ".venv/Scripts/python.exe" scripts/ask_question.py \
  --question "<your specific question with full context>" \
  [--notebook-id "<id-from-notebooks.yaml>"]
```

**Rules:**
- Never hardcode a notebook_id unless a specific notebook is clearly the best
  source. Default: omit `--notebook-id` to query the full library.
- If omitting `--notebook-id`, the skill queries the active notebook or the
  full library. Consult `notebooks.yaml` to identify the best-fit notebook
  and pass its id when relevance is clear.
- Each question opens a new browser session — include full context in the
  question string (do not rely on conversational history).
- If the answer ends with "Is that ALL you need to know?", assess whether
  follow-up is needed before proceeding.

## Notebook Registry Reference

See `skills/notebooklm/notebooks.yaml` for the full registry.

Quick routing guide:
| Topic | Notebook ID |
|-------|-------------|
| Agent design, MAS, orchestration, cost | `ai-agents-&-multi-agent-systems` |
| Agentic AI dev, safety, evaluation | `agentic-ai-systems---development-&-orchestration` |
| Storage, databases, vector DBs, RAG | `database-systems-&-ai-integrated-dbms` |
| ML/DL, transformers, PyTorch | `ml-&-deep-learning-comprehensive-reference` |
| Governance, KPIs, project management | `performance-management-&-project-governance` |
| Knowledge management, Notion/Zotero | `zotero-notion-python-integration` |

## Pattern for Agents WITHOUT Execute Access (Consultants)

Consultants (risk_advisor, quality_advisor, devils_advocate, domain_expert,
efficiency_advisor) cannot invoke the skill directly. Instead:

1. If you need grounded context, state it explicitly in your output:
   ```
   KNOWLEDGE_REQUEST: <specific question>
   SUGGESTED_NOTEBOOK: <notebook-id or "full library">
   ```
2. master_orchestrator will intercept this, fetch the answer, and re-inject
   it into a follow-up consultation request.

## Error Handling

- If the skill returns an error or timeout: note the failure in your output,
  proceed without the grounded answer, and flag that the response is ungrounded.
- Do not retry more than once per question within a single workflow step.
- Authentication errors: report to user — do not attempt to re-authenticate
  autonomously.

## Additive-Only Rule

When adding this invocation pattern to an agent definition:
- Add a new `## Knowledge Retrieval (NotebookLM)` section at the end of
  the agent's workflow steps.
- Do NOT modify, remove, or reorder any existing content.
- The section must follow this structure:

```markdown
## Knowledge Retrieval (NotebookLM)

When grounded external knowledge is needed, invoke the NotebookLM skill
following `skills/notebooklm/TEMPLATE.md`.

**This agent's access type:** [direct | via master_orchestrator broker]

**Typical query triggers for this agent:**
- [list 2-4 specific triggers relevant to this agent's role]

**Suggested notebooks:** [list from notebooks.yaml best_for field]
```
