# Agent architecture

## Roles

The registry contains nine roles:

1. Environmental supervisor — starts the workflow and coordinates work.
2. Air-quality analyst — reads station-level pollutant observations.
3. Weather analyst — reads modelled/observed meteorological records.
4. Satellite/fire analyst — reads NASA FIRMS event records.
5. Environmental-event analyst — reads GDELT contextual news.
6. Prediction analyst — trains/queries the evaluated forecasting baseline.
7. Anomaly investigator — reads recorded station-level anomalies.
8. RAG researcher — retrieves indexed document passages and citations.
9. Risk assessor — reviews collected specialist evidence and uncertainty.

## Graph and state

`backend/agents.py` builds a LangGraph `StateGraph` with a typed investigation
state. The supervisor dispatches the seven specialist data tools in parallel.
The risk node runs after specialist results are collected, and the final
synthesis node runs last. Evidence references are saved with agent tasks,
messages, investigation evidence, and completion checkpoints.

Each specialist queries persisted source records; it does not invent a
provider response. Empty tables produce `no_data` or `insufficient_evidence`.
An agent tool failure is logged and represented as a failed specialist result
so other agents can still contribute.

When `GEMINI_API_KEY` is configured, Gemini 2.5 Flash can synthesize only the
collected evidence with a bounded request timeout and citation instruction.
Evidence text is untrusted input. Without Gemini, the graph returns a
deterministic status and source-record references, not a generated causal
explanation. The graph does not claim that weather, a fire, or a news article
caused a pollution change.

## Limits

The local API currently launches graph execution as an in-process background
task. A process restart can lose a queued investigation; use a durable worker
queue before multi-replica production deployment. Agent checkpoint rows
currently record completed specialist state, not LangGraph's full resumable
checkpoint protocol. Authentication, per-user permissions, and distributed
execution quotas remain production prerequisites.
