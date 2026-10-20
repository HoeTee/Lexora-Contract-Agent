# Offline model tokenizers

These files are tokenizer assets, not model weights. `manifest.json` pins the upstream repository revision and the SHA256 and size of every downloaded file.

| Local directory | Upstream repository |
| --- | --- |
| `bge-m3` | `BAAI/bge-m3` |
| `bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-m3` |
| `qwen3-embedding` | `Qwen/Qwen3-Embedding-8B` |
| `qwen3-reranker` | `Qwen/Qwen3-Reranker-8B` |
| `qwen3.6-35b-a3b` | `Qwen/Qwen3.6-35B-A3B` |

Prepare assets on the connected build machine:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py --verify
```

The download command reuses revisions from the existing manifest. Verification does not download files. Keep the manifest and all five directories together.

The Docker build copies this directory to `/app/resources/tokenizers/` and verifies all five tokenizers with networking disabled. Runtime loading uses `local_files_only=True` and `trust_remote_code=False`. Missing or corrupt assets for these models cause an error; they do not cause a network download or a fallback to another tokenizer.

Use the same application image for the API and all three workers. The deployment host does not need Python or a tokenizer download step. Input limits belong in `embedding.max_input_tokens` and `rerank.max_input_tokens`; the service can enforce a lower limit than the original model capacity.
