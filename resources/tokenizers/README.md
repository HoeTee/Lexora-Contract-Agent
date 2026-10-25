# Offline model tokenizers

These files are tokenizer assets, not model weights. `manifest.json` pins the upstream repository revision and the SHA256 and size of every downloaded file.

| Local directory | Upstream repository |
| --- | --- |
| `bge-m3` | `BAAI/bge-m3` |
| `bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-m3` |
| `qwen3-embedding` | `Qwen/Qwen3-Embedding-8B` |
| `qwen3-reranker` | `Qwen/Qwen3-Reranker-8B` |
| `qwen3.6-35b-a3b` | `Qwen/Qwen3.6-35B-A3B` |
| `deepseek-v4-flash-0731` | `deepseek-ai/DeepSeek-V4-Flash-0731` |

The DeepSeek directory includes its pinned 0731 message encoder and license.
`cl100k_base.tiktoken.gz` is the packaged fallback table; the runtime checks its
SHA256 and loads it directly without a network or external tiktoken cache.

Prepare assets on the connected build machine:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py --verify
```

The download command reuses revisions from the existing manifest. Verification does not download files. Keep the manifest, fallback table and all six directories together.

The Docker build copies this directory to `/app/resources/tokenizers/` and verifies all six tokenizers with networking disabled. Runtime loading uses `local_files_only=True` and `trust_remote_code=False`. The DeepSeek encoder is the checksum-verified local reference file, not dynamically downloaded remote code. Missing or corrupt assets cause an error; they do not cause a network download or fallback to another tokenizer.

Use the same application image for the API and all three workers. The deployment host does not need Python or a tokenizer download step. Model names are matched only through the explicit `tokenizers` configuration; unmatched names use the declared `cl100k_base: [other]` estimate. Input limits belong in `llm.max_input_tokens`, `embedding.max_input_tokens` and `rerank.max_input_tokens`. Tokenizer metadata and `model_max_length` do not impose or clamp these configured limits.
