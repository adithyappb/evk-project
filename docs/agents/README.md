# EVkids Agent Records

OASF-style records for each agent in the EVkids pipeline.
Format follows the [Open Agent Schema Framework](https://oasf.outshift.com) v1.0.0.

## Important note on deployment model

These records follow the OASF schema but EVkids agents are **not** deployed as
independent A2A services. They are Python classes in the same package, wired
together explicitly at startup. The `url` field in each A2A card uses the form
`python://evk.agents.<module>:<Class>` to indicate the current deployment mode.
The records describe what each agent *would* look like if extracted into a
standalone service — useful as capability documentation and for future
decomposition if the architecture grows.

## Pipeline topology

```
Inkbox webhook / poll
        │
        ▼
┌───────────────────┐
│  Ingestion Agent  │  Orchestrates the full classify → deduplicate → personalise flow
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│ Classifier Agent  │  Gemini structured output — extracts typed Opportunity records
└───────┬───────────┘
        │  (confidence ≥ 0.75)
        ▼
┌───────────────────────┐
│  Personalizer Agent   │  Rule-based scoring → Gemini copy → DraftMessage (pending)
└───────────────────────┘
        │
        │  [human approval gate]
        ▼
┌───────────────────────┐
│  Distributor Agent    │  Email / WhatsApp / SMS delivery (APPROVED only)
└───────────────────────┘

Parallel / scheduled agents
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│   Digest Agent   │   │  Reminder Agent  │   │ Web Scraper Agent│
│  (weekly, no LLM)│   │ (hourly, no LLM) │   │ (manual/sched.)  │
└──────────────────┘   └──────────────────┘   └──────────────────┘
```

## Agent summary

| Record | LLM | Human gate | Schedule | Key idempotency |
|--------|-----|-----------|----------|-----------------|
| [ingestion-agent.json](ingestion-agent.json) | No (delegates) | No | Event-driven | `inkbox_message_id` |
| [classifier-agent.json](classifier-agent.json) | Gemini 2.0 Flash | No | On demand | — |
| [personalizer-agent.json](personalizer-agent.json) | Gemini 2.0 Flash | Yes | On demand | `student_id + opportunity_id` |
| [distributor-agent.json](distributor-agent.json) | No | Yes (APPROVED only) | On demand | `draft_id` |
| [digest-agent.json](digest-agent.json) | No | Yes | Weekly (ISO week) | `student_id + ISO_week` |
| [reminder-agent.json](reminder-agent.json) | No | No | Hourly | `student_id + opp_id + days_before` |
| [web-scraper-agent.json](web-scraper-agent.json) | No (delegates) | Yes | Manual / scheduled | — |

## EVkids-specific extension fields

Each record includes an `extensions` object with keys prefixed `evkids:`.
These are not part of the OASF standard — they document pipeline-specific
behaviour that the standard schema doesn't have a field for:

| Extension key | Purpose |
|---|---|
| `evkids:deployment` | `python_direct` — in-process, not a network service |
| `evkids:gemini_dependency` | Whether this agent calls Gemini directly |
| `evkids:gemini_model` | Exact model string used |
| `evkids:human_gate` | Whether output requires admin approval before acting |
| `evkids:idempotency_key` | Field(s) that prevent duplicate processing |
| `evkids:pii_handling` | How student PII is handled before leaving the process |
| `evkids:downstream_agents` | Which agents are called next in the pipeline |
