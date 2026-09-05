# Qwen in OpenCode

The installed provider is `qwen38/qwen38`. Fast mode is the default; select
the `thinking` variant in OpenCode when additional reasoning is worth the wait.
Model options use `chat_template_kwargs`, not the CLI `--thinking` display flag.

Local source templates: `qwen38-opencode.json`, `qwen38-session`, and
`qwen38-tunnel.service`. Merge the provider template into the existing OpenCode
configuration; do not replace unrelated providers or settings.

The user service runs the session helper, which delegates lifecycle and tunnel
management to modelctl. It checks health every 15 seconds and reconnects when
needed. Start/stop/restart with `qwen38-session`; stopping the helper service
before stopping the model prevents recovery from undoing an intentional stop.
An enabled service starts at user-session startup. Remote machine cold starts
still require model loading; this setup does not promise instant boot recovery.

Applied server settings in the private modelctl target configuration:

- 131072 maximum context; OpenCode compacts using a conservative 98304 limit.
- 4096 OpenCode output budget; existing compaction reserve retained.
- 4 maximum concurrent sequences and 2048 tokens per prefill batch.
- `--performance-mode interactivity` and explicit prefix caching.
- `--enable-prompt-tokens-details` for observable cache reuse.
- Fast chat-template defaults; per-request thinking variants override these.
- Existing INT8 weights, FP8 KV cache, two-GPU parallelism, and MTP-1 retained.

`qwen-latency-probe.py` measures streamed first-text latency and cached prompt
tokens using synthetic input. It prints metrics only. Real latency depends on
prompt size, concurrent requests, output length, and the selected thinking mode.
