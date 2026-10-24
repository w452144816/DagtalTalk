# SoulX-FlashHead-1.3B（Lite）推理服务：OpenTalking 的 flashhead 模型只是客户端，推理要自己起（AUDI/VIDX WebSocket 协议，:8766）
# 构建上下文（由 deploy.sh 准备）：/data/flashhead/build/{Dockerfile,flashhead_server.py,flash_attn-*.whl}
# 代码和权重运行时挂载：/data/flashhead/src/SoulX-FlashHead → /app/SoulX-FlashHead，/data/flashhead/models → /w/models
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel
# uv 不认 PIP_INDEX_URL，要单独给 UV_INDEX_URL，否则直连 pypi.org（国内会卡住几个小时不动）
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple UV_HTTP_TIMEOUT=600 UV_CONCURRENT_DOWNLOADS=16 DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
# 国内：apt 换阿里源（基础镜像是 Ubuntu 22.04，源文件是 sources.list）
RUN sed -i 's|http://archive.ubuntu.com|https://mirrors.aliyun.com|g; s|http://security.ubuntu.com|https://mirrors.aliyun.com|g' /etc/apt/sources.list \
 && apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install -q uv
# 上游 requirements（去掉 gradio/flask 这类服务用不到的；xformers 0.0.31 对应 torch 2.7.1；mediapipe 要 numpy<2，所以 opencv 压到 4.11）
RUN uv pip install --system "diffusers>=0.34.0" "transformers==4.57.3" "tokenizers>=0.20.3" "accelerate>=1.8.1" \
      tqdm imageio imageio-ffmpeg easydict ftfy scikit-image loguru pyloudnorm decord librosa soundfile \
      "opencv-python-headless>=4.9,<4.12" "mediapipe==0.10.21" "xformers==0.0.31" einops av websockets "numpy<2" \
 && uv pip install --system "nvidia-nccl-cu12==2.27.3"
# FlashHead 模型代码 import 了 xfuser（多卡并行库，单卡也要能 import）；锁住 torch 不让它被换掉
RUN echo "torch==2.7.1" > /tmp/c.txt && echo "torchvision==0.22.1" >> /tmp/c.txt \
 && echo "xformers==0.0.31" >> /tmp/c.txt && echo "numpy<2" >> /tmp/c.txt \
 && uv pip install --system -c /tmp/c.txt "xfuser>=0.4.3"
# 基础镜像带 CUDA 12.8 前向兼容库（只给数据中心卡用），GeForce（4090/3090）上会报 Error 804，必须删掉
RUN rm -rf /usr/local/cuda/compat /usr/local/cuda-*/compat && ldconfig
COPY flash_attn-*.whl /tmp/
RUN uv pip install --system /tmp/flash_attn-*.whl && rm /tmp/flash_attn-*.whl
COPY flashhead_server.py /app/flashhead_server.py
WORKDIR /app
ENV FLASHHEAD_SRC=/app/SoulX-FlashHead FLASHHEAD_MODEL_DIR=/w/models/SoulX-FlashHead-1_3B FLASHHEAD_WAV2VEC_DIR=/w/models/wav2vec2-base-960h
CMD ["python", "/app/flashhead_server.py"]
