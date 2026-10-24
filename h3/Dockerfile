# MiniMax H3 本地试跑（ComfyUI 原生节点）。205：2×3090、驱动 535（CUDA 12.2）、内存 62G
# 构建上下文：ComfyUI/（git clone Comfy-Org/ComfyUI）；权重挂载到 /w/models
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-devel
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_HTTP_TIMEOUT=300 PYTHONUNBUFFERED=1
RUN pip install -q uv && apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg libgl1 && rm -rf /var/lib/apt/lists/*
COPY ComfyUI/requirements.txt /tmp/req.txt
# 工作流模板 / 内嵌文档包只给网页 UI 用（我们走 API），且清华源还没同步它的素材包 → 去掉
# comfy-kitchen（INT8/NVFP4 量化算子）在 torch 2.5 上注册自定义算子失败，要新 torch；
# torch 2.8 的 PyPI 版是 CUDA 12.8 构建，靠 CUDA 12.x 小版本兼容跑在 535 驱动上（3090 有现成 sm86 代码，不用 PTX JIT）
RUN echo "torch==2.8.0" > /tmp/c.txt && echo "torchvision==0.23.0" >> /tmp/c.txt && echo "torchaudio==2.8.0" >> /tmp/c.txt && \
    grep -vE "^comfyui-(workflow-templates|embedded-docs)" /tmp/req.txt > /tmp/req2.txt && \
    uv pip install --system -c /tmp/c.txt -r /tmp/req2.txt
COPY ComfyUI /opt/ComfyUI
WORKDIR /opt/ComfyUI
CMD ["python", "main.py", "--listen", "127.0.0.1", "--port", "8188", "--output-directory", "/w/output"]
