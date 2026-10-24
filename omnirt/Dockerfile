# OmniRT + FasterLivePortrait（JoyVASA 音频驱动表情/头动 + LivePortrait 变形）推理服务，OpenTalking fasterliveportrait 模型的后端（:9000）
# 基础镜像：FasterLivePortrait 作者的 docker（TensorRT 8 + grid_sample3d 插件 + 定制 onnxruntime-gpu），HF 上的插件 .so 就是按它编的
# 源码和权重运行时挂载：/data/omnirt/src/omnirt → /app/omnirt，/data/omnirt/src/FasterLivePortrait → /app/FasterLivePortrait，
#   /data/omnirt/checkpoints → /w/checkpoints（liveportrait_onnx、JoyVASA、chinese-hubert-base；TRT 引擎首启时转好也落在这里）
FROM shaoguo/faster_liveportrait:v3
USER root
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple PYTHONUNBUFFERED=1
# OmniRT 本体只要轻量依赖 + server 套件；torch/tensorrt/onnxruntime 用基础镜像里的，不动
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.5" "python-multipart>=0.0.9" \
      "sse-starlette>=2.0" "grpcio>=1.60" "protobuf>=5.26" "httpx>=0.26" "imageio>=2.34" "imageio-ffmpeg>=0.4.9" \
      PyYAML Pillow munch omegaconf soundfile librosa "transformers>=4.40,<4.50" torchgeometry
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PYTHONPATH=/app/omnirt/src
CMD ["/entrypoint.sh"]
