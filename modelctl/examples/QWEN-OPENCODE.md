# Qwen in OpenCode

The installed provider is `qwen38/qwen38`, configured as the OpenCode default
with maximum (`xhigh`) thinking enabled by default. There are no custom speed
or effort toggles; the model uses one consistent configuration for code quality
and long sessions.
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

- 262144 maximum context (the model's native 256 Ki-token limit); OpenCode
  compacts only when it approaches that boundary.
- 8192 OpenCode output budget; existing compaction reserve retained.
- 8 maximum concurrent sequences and 8192 tokens per prefill batch.
- `--performance-mode throughput` and explicit prefix caching.
- `--enable-prompt-tokens-details` for observable cache reuse.
- Thinking chat-template defaults explicitly select `xhigh`; MTP is disabled
  for high-concurrency throughput.
- Existing INT8 weights, FP8 KV cache, and two-GPU tensor parallelism retained.

`qwen-latency-probe.py` measures streamed first-text latency and cached prompt
tokens using synthetic input. It prints metrics only. Real latency depends on
prompt size, concurrent requests, output length, and the selected thinking mode.
