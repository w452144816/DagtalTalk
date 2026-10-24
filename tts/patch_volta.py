"""给装好的 nano-vllm-voxcpm 打补丁，让它能在 Volta（TITAN V / V100，sm70）上跑：
1. 没装 flash-attn（Volta 不支持）或 VOX_ATTN=sdpa 时，注意力换成 attention_sdpa（PyTorch SDPA）
2. 计算精度可由 VOX_DTYPE 指定（Volta 没有 bf16，用 float16）；不设时保持原来的 bfloat16
构建时运行：python patch_volta.py <site-packages/nanovllm_voxcpm>
"""
import shutil
import sys
from pathlib import Path

pkg = Path(sys.argv[1])
shutil.copy(Path(__file__).with_name("attention_sdpa.py"), pkg / "layers" / "attention_sdpa.py")


def sub(path: Path, old: str, new: str) -> None:
    s = path.read_text(encoding="utf-8")
    if new in s:
        return
    assert s.count(old) == 1, f"{path}: 找不到要替换的代码 {old!r}"
    path.write_text(s.replace(old, new), encoding="utf-8")


sub(pkg / "layers" / "attention.py",
    "from flash_attn import flash_attn_varlen_func, flash_attn_with_kvcache, flash_attn_func\n",
    "import os\n"
    "try:\n"
    "    if os.getenv('VOX_ATTN') == 'sdpa':\n"
    "        raise ImportError\n"
    "    from flash_attn import flash_attn_varlen_func, flash_attn_with_kvcache, flash_attn_func\n"
    "except ImportError:\n"
    "    from nanovllm_voxcpm.layers.attention_sdpa import (flash_attn_varlen_func, flash_attn_with_kvcache,\n"
    "                                                       flash_attn_func)\n")

sub(pkg / "llm.py",       # 启动时的 flash-attn 依赖检查：没装就走 SDPA，不报错
    "    import flash_attn  # noqa: F401\nexcept ImportError:\n    raise ImportError(\n",
    "    import flash_attn  # noqa: F401\nexcept ImportError:\n    pass\nif False:\n    raise ImportError(\n")

# LongRoPE 的 Triton kernel 把 fp32 结果存回 bf16（cvt.bf16.f32 要 sm80+）：Volta 上换成等价的 PyTorch 实现
lr = pkg / "layers" / "longrope.py"
if "_apply_longrope_torch" not in lr.read_text(encoding="utf-8"):
    lr.write_text(lr.read_text(encoding="utf-8") + '''

def _apply_longrope_torch(positions, query, key, cos_cache, sin_cache, head_dim):
    """和 _longrope_kernel 同样的 FP32 运算（两个乘积分别舍入，不用 FMA），给不能编译 bf16 Triton 的老卡用。"""
    pos = positions.reshape(-1).long()
    cos = cos_cache[pos].float()[:, None, :]
    sin = sin_cache[pos].float()[:, None, :]
    half = head_dim // 2

    def rot(x):
        xf = x.reshape(pos.numel(), -1, head_dim).float()
        xr = torch.cat([-xf[..., half:], xf[..., :half]], dim=-1)
        return ((xf * cos) + (xr * sin)).to(x.dtype).reshape(x.shape)

    return rot(query), rot(key)


import os as _os
if _os.getenv("VOX_ATTN") == "sdpa":
    apply_longrope = _apply_longrope_torch
''', encoding="utf-8")

# KV cache 用 zeros 分配：SDPA 版 decode 会 gather 到还没写过的位置（靠 mask 置 0 权重），
# torch.empty 的垃圾值里可能有 NaN，0×NaN 仍是 NaN → 生成停不下来。flash-attn 不读这些位置，所以原版无所谓
mr = pkg / "engine" / "model_runner.py"
s = mr.read_text(encoding="utf-8")
s = s.replace("module.k_cache = torch.empty(", "module.k_cache = torch.zeros(")
s = s.replace("module.v_cache = torch.empty(", "module.v_cache = torch.zeros(")
mr.write_text(s, encoding="utf-8")

# fp16 时残差流留在 fp32：模型没用 muP 缩放，28 层累加后残差会超过 fp16 上限（65504）→ inf/NaN
sub(pkg / "layers" / "layernorm.py",
    "        residual = x.to(orig_dtype)\n",
    "        residual = x if orig_dtype == torch.float16 else x.to(orig_dtype)\n")
# 无残差的 RMSNorm 在 fp16 里直接算：|x|>256 时 x² 溢出 → 方差 inf → 输出全 0（不是 inf，查溢出也看不出来）
sub(pkg / "layers" / "layernorm.py",
    "        return F.rms_norm(x, (self.hidden_size,), self.weight, self.eps)\n",
    "        if x.dtype == torch.float16:\n"
    "            return F.rms_norm(x.float(), (self.hidden_size,), self.weight.float(), self.eps).to(x.dtype)\n"
    "        return F.rms_norm(x, (self.hidden_size,), self.weight, self.eps)\n")

runner = pkg / "models" / "voxcpm2" / "runner.py"
sub(runner, "    def dtype(self) -> torch.dtype:\n        return torch.bfloat16\n",
    "    def dtype(self) -> torch.dtype:\n        import os\n"
    "        return getattr(torch, os.getenv('VOX_DTYPE', 'bfloat16'))\n")
sub(runner, "        torch.set_default_dtype(torch.bfloat16)\n", "        torch.set_default_dtype(self.dtype)\n")
print("patched", pkg)
