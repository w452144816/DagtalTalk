# 本地语音合成服务（VoxCPM2 + nano-vllm-voxcpm）。torch 锁 2.5.1+cu121：服务器驱动 535 只到 CUDA 12.2
# 构建上下文需要：server.py、patch_volta.py、attention_sdpa.py、nanovllm-voxcpm/（git clone）
# 可选：flash_attn 预编译 wheel（Ampere 及以上用；服务器直连 GitHub 下载会断，本地下好传上去）。
#       不放 wheel 就走 PyTorch SDPA（Volta/TITAN V 只能这样），运行时再配 VOX_DTYPE=float16
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-devel
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_HTTP_TIMEOUT=300 PYTHONUNBUFFERED=1
RUN pip install -q uv
COPY patch_volta.py attention_sdpa.py flash_attn-*.whl* /tmp/
RUN echo "torch==2.5.1" > /tmp/c.txt && echo "torchaudio==2.5.1" >> /tmp/c.txt && echo "triton==3.1.0" >> /tmp/c.txt && \
    uv pip install --system -c /tmp/c.txt "transformers>=4.51,<4.58" xxhash tqdm numpy librosa \
      pydantic "soundfile>=0.13.1" soxr fastapi "uvicorn[standard]" httpx && \
    if ls /tmp/flash_attn-*.whl >/dev/null 2>&1; then uv pip install --system -c /tmp/c.txt /tmp/flash_attn-*.whl; fi && \
    rm -f /tmp/flash_attn-*.whl
COPY nanovllm-voxcpm /opt/nanovllm-voxcpm
RUN SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 uv pip install --system --no-deps -c /tmp/c.txt /opt/nanovllm-voxcpm && \
    python /tmp/patch_volta.py "$(python -c 'import site; print(site.getsitepackages()[0])')/nanovllm_voxcpm"
COPY server.py /app/server.py
WORKDIR /app
CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8302"]
