"""测试 attn_implementation 对 16K prompt prefill 显存的影响。"""
import os, sys, time
import torch

# patch warmup
import transformers.modeling_utils as _tmu
_tmu.caching_allocator_warmup = lambda *a, **k: None

from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "opendatalab/MinerU-HTML-v1.1-hunyuan0.5B-compact"
ATTN = sys.argv[1] if len(sys.argv) > 1 else "sdpa"  # sdpa / eager / flash_attention_2
PROMPT_TOKENS = int(sys.argv[2]) if len(sys.argv) > 2 else 16384

print(f"=== attn={ATTN} prompt_tokens={PROMPT_TOKENS} ===", flush=True)
print(f"GPU0 free / total: {torch.cuda.mem_get_info(0)[0]/1e9:.2f} / {torch.cuda.mem_get_info(0)[1]/1e9:.2f} GB", flush=True)

print("loading model...", flush=True)
t0 = time.time()
try:
    model = AutoModelForCausalLM.from_pretrained(
        MODEL,
        trust_remote_code=True,
        device_map="cuda:0",
        dtype=torch.float16,
        max_memory={0: "5GiB", "cpu": "16GiB"},
        attn_implementation=ATTN,
    )
except Exception as e:
    print(f"[FAIL] load with attn={ATTN}: {e}", flush=True)
    sys.exit(2)
model.eval()
print(f"loaded in {time.time()-t0:.1f}s, alloc={torch.cuda.memory_allocated(0)/1e9:.2f} GB", flush=True)
print(f"actual attn_impl: {model.config._attn_implementation}", flush=True)

tok = AutoTokenizer.from_pretrained(MODEL)
# 构造一个 PROMPT_TOKENS 长度的输入
input_ids = torch.randint(low=10, high=tok.vocab_size - 100, size=(1, PROMPT_TOKENS), device="cuda:0")
attn = torch.ones_like(input_ids)
print(f"prefill prompt_tokens={input_ids.shape[-1]}, alloc_before={torch.cuda.memory_allocated(0)/1e9:.2f} GB", flush=True)

t1 = time.time()
try:
    with torch.no_grad():
        out = model.generate(
            input_ids=input_ids,
            attention_mask=attn,
            max_new_tokens=8,
            do_sample=False,
            pad_token_id=tok.pad_token_id or tok.eos_token_id,
        )
    peak = torch.cuda.max_memory_allocated(0) / 1e9
    print(f"[OK] generate in {time.time()-t1:.1f}s; peak_alloc={peak:.2f} GB; new_tokens={out.shape[-1]-input_ids.shape[-1]}", flush=True)
except torch.cuda.OutOfMemoryError as e:
    print(f"[OOM] {str(e)[:200]}", flush=True)
    sys.exit(3)
except Exception as e:
    print(f"[ERR] {type(e).__name__}: {str(e)[:200]}", flush=True)
    sys.exit(4)
