"""flash-attn 的三个函数换成 PyTorch SDPA 实现，给不支持 flash-attn 的老卡（Volta / TITAN V，sm70）用。

只覆盖 nano-vllm-voxcpm 用到的参数组合，接口、张量形状和 flash-attn 一致：
- flash_attn_func(q, k, v)：非因果，(B, S, H, D)
- flash_attn_varlen_func：prefill，按 cu_seqlens 拼在一起的变长序列；可带 block_table（前缀缓存，k/v 是分页缓存）
- flash_attn_with_kvcache：decode，每条 1 个 query，k/v 是分页缓存 (num_blocks, block_size, kv_heads, D)
decode 会被 CUDA graph 捕获，所以不能有按实际长度变化的形状：按 block_table 的全部列 gather，再用 cache_seqlens 做 mask。
因果 mask 按 flash-attn 的右下角对齐（query 比 key 短时对齐到末尾）。
"""
from __future__ import annotations

import torch
import torch.nn.functional as F


def _rep(x: torch.Tensor, n: int) -> torch.Tensor:
    """GQA：kv 头复制到和 q 头数一样（dim=1 是头）。"""
    return x if n == 1 else x.repeat_interleave(n, dim=1)


def flash_attn_func(q, k, v, softmax_scale=None, causal=False):
    qh, kh, vh = q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
    g = qh.shape[1] // kh.shape[1]
    o = F.scaled_dot_product_attention(qh, _rep(kh, g), _rep(vh, g), is_causal=causal, scale=softmax_scale)
    return o.transpose(1, 2).contiguous()       # 调用方会 .view()，要和 flash-attn 一样连续


def _paged(cache: torch.Tensor, blocks: torch.Tensor, n: int) -> torch.Tensor:
    """分页缓存里取出一条序列的前 n 个位置 → (n, kv_heads, D)。"""
    return cache[blocks.long()].reshape(-1, cache.shape[-2], cache.shape[-1])[:n]


def flash_attn_varlen_func(q, k, v, max_seqlen_q=None, cu_seqlens_q=None, max_seqlen_k=None, cu_seqlens_k=None,
                           softmax_scale=None, causal=True, block_table=None):
    cq, ck = cu_seqlens_q.tolist(), cu_seqlens_k.tolist()
    out = torch.empty_like(q)
    for i in range(len(cq) - 1):
        lq, lk = cq[i + 1] - cq[i], ck[i + 1] - ck[i]
        if lq == 0:
            continue
        qi = q[cq[i]:cq[i + 1]]
        if block_table is not None:
            bs = k.shape[1]
            nb = (lk + bs - 1) // bs
            ki, vi = _paged(k, block_table[i, :nb], lk), _paged(v, block_table[i, :nb], lk)
        else:
            ki, vi = k[ck[i]:ck[i + 1]], v[ck[i]:ck[i + 1]]
        qh, kh, vh = qi.transpose(0, 1)[None], ki.transpose(0, 1)[None], vi.transpose(0, 1)[None]
        g = qh.shape[1] // kh.shape[1]
        mask = None
        if causal and lq != lk:
            r = torch.arange(lq, device=q.device)[:, None] + (lk - lq)
            mask = torch.arange(lk, device=q.device)[None, :] <= r
        o = F.scaled_dot_product_attention(qh, _rep(kh, g), _rep(vh, g), attn_mask=mask,
                                           is_causal=causal and lq == lk, scale=softmax_scale)
        out[cq[i]:cq[i + 1]] = o[0].transpose(0, 1)
    return out


def flash_attn_with_kvcache(q, k_cache, v_cache, cache_seqlens=None, block_table=None, softmax_scale=None,
                            causal=True):
    # q: (B, 1, H, D)；block_table: (B, max_blocks)，-1 补齐
    b = q.shape[0]
    bt = block_table.clamp(min=0).long()
    kv_h, d = k_cache.shape[-2], k_cache.shape[-1]
    kk = k_cache[bt].reshape(b, -1, kv_h, d).transpose(1, 2)        # (B, kvh, L, D)
    vv = v_cache[bt].reshape(b, -1, kv_h, d).transpose(1, 2)
    length = kk.shape[2]
    mask = (torch.arange(length, device=q.device)[None, :] < cache_seqlens[:, None].long())[:, None, None, :]
    # GQA 不复制 kv 头：同一组的 g 个 q 头当成 g 个 query（decode 每头只有 1 个 query，看的是同一段 kv）
    h = q.shape[2]
    g = h // kv_h
    qg = q[:, 0].reshape(b, kv_h, g, d)                                 # (B, kvh, g, D)
    o = F.scaled_dot_product_attention(qg, kk, vv, attn_mask=mask, scale=softmax_scale)
    return o.reshape(b, 1, h, d).contiguous()                           # (B, 1, H, D)
