# 实时数字人 demo：OpenTalking(QuickTalk local + SenseVoice + edge TTS) + 网关页面
# 基础镜像自带 py3.11 / torch 2.5.1+cu121 / cuDNN9（服务器驱动 535 = CUDA 12.2，不能用 cu128 的新 torch）
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

RUN sed -i 's@//archive.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g; s@//security.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list \
 && apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 openssl curl \
 && rm -rf /var/lib/apt/lists/*

# 锁住镜像自带的 torch（防止被升级成 cu12.8 版）；transformers 5.x 要新 torch，不锁会无限回溯
RUN printf 'torch==2.5.1\ntorchvision==0.20.1\ntorchaudio==2.5.1\ntransformers==4.57.3\n' > /etc/pip-constraints.txt
ENV PIP_CONSTRAINT=/etc/pip-constraints.txt

WORKDIR /app/opentalking
# 先只装依赖（pyproject 不变就命中缓存），再拷源码
COPY opentalking/pyproject.toml opentalking/README.md opentalking/LICENSE ./
# 这条网络对 HTTP/1.1 单连接限速约 0.4MB/s（HTTP/2 不限），pip 只能串行慢慢下；
# 用 uv 多连接并行下载
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple UV_CONCURRENT_DOWNLOADS=48 UV_HTTP_TIMEOUT=600 \
    UV_LINK_MODE=copy UV_NO_CACHE=1
RUN pip install uv \
 && mkdir -p opentalking apps && touch opentalking/__init__.py apps/__init__.py \
 && uv pip install --system -c /etc/pip-constraints.txt ".[models,quicktalk-cuda,local-audio]" \
 && uv pip uninstall --system opentalking
# 本地克隆音色：Qwen3-TTS + CUDA graph 加速。--no-deps：qwen-tts 会拖进 CPU 版 onnxruntime / gradio；
# faster-qwen3-tts 0.4 起要 transformers 5（要新 torch），只能用 0.3.x
RUN uv pip install --system --no-deps qwen-tts==0.1.1 faster-qwen3-tts==0.3.2 sox==1.5.0 einops==0.8.2
# 人设素材解析（gateway/persona_builder.py）：PDF / Word 抽正文
RUN uv pip install --system pypdf==6.1.1 python-docx==1.2.0
COPY opentalking/ ./
COPY patches/ /app/patches/
RUN python /app/patches/opentalking_patch.py /app/opentalking
RUN pip install --no-deps -e . \
 && ln -s /w/data /app/opentalking/data && chmod -R a+rwX /app/opentalking   # 容器以 wwang 身份跑，OpenTalking 会往 cwd 写东西

COPY gateway/ /app/gateway/
COPY scripts/smoke.py /app/scripts/smoke.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV HOME=/w/cache HF_HOME=/w/cache/hf MODELSCOPE_CACHE=/w/cache/modelscope PYTHONNOUSERSITE=1 \
    OPENTALKING_ENV_FILE=/dev/null
EXPOSE 8443
ENTRYPOINT ["/app/entrypoint.sh"]
