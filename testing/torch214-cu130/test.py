import modal

app = modal.App("torch214-cu130-blackwell-test")
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.3-devel-ubuntu22.04", add_python="3.11")
    .entrypoint([])
    .run_commands("python -m pip install --upgrade pip")
    .run_commands("python -m pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu130")
)

@app.function(image=image, gpu="RTX-PRO-6000", timeout=300)
def verify():
    import torch
    print("Torch:", torch.__version__, "CUDA:", torch.version.cuda)
    print("GPU:", torch.cuda.get_device_name(0), "capability:", torch.cuda.get_device_capability(0))
    x = torch.randn((1024, 1024), device="cuda", dtype=torch.bfloat16)
    y = x @ x
    torch.cuda.synchronize()
    print("BF16 matmul:", y.shape, torch.isfinite(y).all().item())
    q = torch.randn((1, 4, 128, 64), device="cuda", dtype=torch.bfloat16)
    z = torch.nn.functional.scaled_dot_product_attention(q, q, q)
    torch.cuda.synchronize()
    print("SDPA:", z.shape, torch.isfinite(z).all().item())
    assert torch.__version__.startswith("2.14.1+cu130")
    return "PASS"

@app.local_entrypoint()
def main():
    print("RESULT:",verify.remote())
