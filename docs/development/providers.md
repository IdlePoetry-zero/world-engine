# Model provider adapters

This package implements typed model calls for DeepSeek Chat Completions, generic Chat Completions compatible endpoints, native Ollama, and native Anthropic Messages. All provider HTTP calls use `httpx`; structured responses are checked locally with `jsonschema` before they can become a `ModelResult`.

## Gateway interface

Application code imports from `world_engine.adapters.models`:

```python
from world_engine.adapters.models import (
    ChatMessage,
    DataPolicy,
    ModelGateway,
    ModelRequest,
    ProviderConfig,
    ProviderKind,
    RoleTask,
)

gateway = ModelGateway(secret_resolver=server_secret_resolver)
models = await gateway.discover(provider_config)
result = await gateway.generate(
    provider_config,
    ModelRequest(
        role_task=RoleTask.GM_ANSWER,
        messages=(ChatMessage("user", "What can my character know?"),),
        data_policy=DataPolicy.CLOUD_ALLOWED,
    ),
)
```

`ModelRequest` carries role task, typed messages, optional `schema_ref` / resolved `output_schema`, required capabilities, independent `speed` and `depth`, bounded timeout policy, trace context, and data policy. `ModelResult` carries normalized output, provider-reported usage, optional estimated usage, a separate cost record, finish reason, timings, validation status, and a complete `CallTrace`.

For streaming, `gateway.stream(config, request)` yields `STARTED`, provisional `TEXT_DELTA`, optional provider-reported `USAGE`, then a terminal `COMPLETED` event containing the validated `ModelResult`. A stream without a valid terminal event raises a normalized `ProviderError` with its trace. Deltas are provisional; callers must not commit them as world facts or final narrative. Cancelling a generation propagates `asyncio.CancelledError` with a safe `call_trace` attribute so the operation layer can persist the attempt before re-raising.

`to_jsonable(record)` and `record.to_public_dict()` serialize immutable adapter records without `dataclasses.asdict`; they handle mapping proxies, enums, decimals and timestamps. Traces contain identifiers, timing, usage, costs and errors. They do not contain messages, output text or resolved credentials.

## Provider configuration and secrets

`ProviderConfig` contains provider ID, protocol kind, base URL, default model, `secret_ref`, retry count and explicit capability overrides. `public_dict()` emits only reference/configuration fields. It rejects URL credentials, query strings, fragments and non-loopback HTTP unless `allow_insecure_http=True`. `DataPolicy.LOCAL_ONLY` accepts only loopback endpoints, based on the URL host rather than the protocol label.

The adapters never accept an API key directly in configuration. `EnvironmentSecretResolver` reads exactly one `env:NAME` reference from a server-configured allowlist and does not enumerate environment variables. `OSCredentialSecretResolver` adapts a host credential-store lookup for exact allowlisted `os:REF` identifiers; a platform-specific credential implementation can be injected without coupling the adapter package to a credential-store library. Keys are resolved only when constructing a request header. No adapter logs request bodies or surfaces raw HTTP exception text. Provider error text and request IDs are sanitized.

The application owns the environment/OS reference allowlist before accepting a provider record from a client. Never allow a client to enumerate arbitrary environment variables or to submit raw key material as `secret_ref`.

## Discovery and capabilities

Discovery reads model catalogs and preserves capability evidence (`state`, source, probe result and time). Capabilities use supported / unsupported / unknown states. Unknown is not treated as support.

| Adapter | Discovery and generation | Capability behavior |
| --- | --- | --- |
| DeepSeek | `GET /models`, `POST /chat/completions`; default model `deepseek-flash`, base URL `https://api.deepseek.com` | `deepseek-flash` protocol facts follow the cited official catalog: streaming, JSON mode, tool use and thinking are supported; strict JSON Schema is not documented by Chat Completions. Context/output limits are taken from the current model catalog and may change. |
| Generic compatible | `GET /models`, `POST /chat/completions` | Text chat follows the configured protocol. All other capabilities remain unknown until an operator override or a connection probe confirms them. This adapter does not send structured-output or streaming parameters when capability support is unknown. |
| Ollama | `GET /api/tags`, optional per-model `POST /api/show`, `POST /api/chat` | Model-specific tool, vision, embedding and thinking facts come from the model information returned by the running service. Chat streaming, JSON Schema format and usage fields follow the documented endpoint contract. The adapter does not start or stop Ollama. |
| Anthropic | Paginated `GET /v1/models`, `POST /v1/messages` | Model-list capability results are used when present. The native Messages adapter uses `output_config.format` for JSON Schema. Model-specific thinking and effort controls are only sent when reported as supported. |

Generic capability overrides use the capability names in `Capability`, for example `{"streaming": true, "json_schema": true}`. Set these only after probing the actual endpoint/model. Provider catalog discovery means the model was listed; it does not mean generation quality, structured compliance or latency has been validated.

## Profiles, routing, retry and validation

`execution_profile(speed, depth)` preserves the design baseline for the nine combinations. Depth controls candidate count, memory limit, causal expansion, semantic review, macro step size and reflection. Speed controls scheduling and output strategy. Speed never modifies temperature. The role-routing table is explicit and ordered: primary and fallback providers are configured by role, and only those configured targets can be used. The baseline helper maps text tasks to DeepSeek `deepseek-flash`; it does not invent an embedding route. A local-only request filters out every non-loopback endpoint, including configured cloud fallbacks.

Timeouts are finite (default connection 10 s, read 180 s, whole call 600 s); each field is bounded in `TimeoutPolicy`. Retry count is 0–2 retries. Only network/timeouts, HTTP 429 and HTTP 5xx can retry. A 429 `Retry-After` hint is honored within the call deadline; other retries use bounded jittered backoff. Authentication, schema, capability and provider client errors do not retry. A stream can retry only before it yields any event; after the first provider event the adapter fails without replaying already visible text.

The gateway checks the configured JSON Schema before dispatch. After the provider returns, it rejects empty text, truncated output, malformed JSON and schema mismatches. There is no hidden output repair retry. Provider JSON mode is not taken as proof that it satisfies the domain schema; local validation remains authoritative.

## Usage, cost and traces

`ProviderUsage.source` is `reported` only when counters are returned by the provider. Missing counters are `None`, never fabricated zeroes. Input, cache, non-cache, output, reasoning and total fields remain distinct. Anthropic cache counters are not added twice; reasoning tokens remain part of provider output semantics. Ollama durations are kept as timing metrics, while token counters stay separate.

`CostRecord` distinguishes `reported`, `estimated` and `unknown`. Current adapters do not claim an API's token counts are a provider-reported dollar amount. The gateway only estimates when the application supplies a versioned `PriceVersion` and required token/cache splits and rates are known; otherwise cost is unknown. Failed or cancelled requests retain attempts and `possibly_billed` status even when usage/cost could not be received.

Each trace includes request and parent IDs, world/branch/job, role, provider, requested and reported model, speed/depth/data-policy, queue/first-token/validation timings, per-attempt duration/status/retryability/request ID/billing uncertainty/usage, finish reason, normalized error, usage and cost. Attempt traces are available from successful results and from `ProviderError.call_trace`; cancellation keeps the trace on the propagated cancellation object.

## Official protocol references

The adapter request fields and capability evidence are based on these primary references:

- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/), [model catalog](https://api-docs.deepseek.com/api/list-models/), and [models and pricing](https://api-docs.deepseek.com/quick_start/pricing/). These currently document `https://api.deepseek.com`, `deepseek-flash`, reasoning effort, JSON object mode, streaming and usage.
- [Ollama chat API](https://docs.ollama.com/api/chat) and [model tags API](https://docs.ollama.com/api/tags). These document `/api/chat`, JSON/JSON Schema format, `done` usage/timing fields, `/api/tags`, and the model-specific `/api/show` reference for thinking support.
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages), [streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming), [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), and [model list](https://platform.claude.com/docs/en/api/models/list). The request includes the documented API version `2026-10-21`; update it from Anthropic's versioning guidance when that version is retired.
- [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) is the protocol reference for the generic compatible adapter only. A compatible endpoint may implement a subset, so its extra capabilities stay unknown by default.

## Test scope and verification boundary

`tests/providers/test_model_gateway.py` uses `httpx.MockTransport` responses to exercise request paths, native payloads, discovery, SSE/NDJSON normalization, usage, schema validation, bounded retry, timeouts, cancellation traces, secret redaction and local-only routing. These are unit/contract tests with mock transports only. No real API credentials were supplied, no paid API call was made, and no Ollama process was started. Real endpoint behavior, provider quality, throughput, cancellation billing and model-specific context limits remain unverified.
