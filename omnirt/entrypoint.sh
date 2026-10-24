#!/bin/bash
# 1) FasterLivePortrait 的代码按相对路径 ./checkpoints 找权重：把挂载的 /w/checkpoints 链进去
# 2) 首次启动把 ONNX 转成本卡的 TRT 引擎（每个 1~5 分钟，结果落在挂载目录，之后复用）
# 3) 起 OmniRT avatar 服务
set -e
cd /app/FasterLivePortrait
[ -e checkpoints ] || ln -s /w/checkpoints checkpoints
CK=checkpoints/liveportrait_onnx
for m in warping_spade-fix landmark retinaface_det_static face_2dpose_106_static appearance_feature_extractor stitching stitching_eye stitching_lip; do
  [ -f "$CK/$m.trt" ] || { echo "convert $m"; python scripts/onnx2trt.py -o "$CK/$m.onnx"; }
done
[ -f "$CK/motion_extractor.trt" ] || { echo "convert motion_extractor (fp32)"; python scripts/onnx2trt.py -o "$CK/motion_extractor.onnx" -p fp32; }
cd /app/omnirt
exec python -c "from omnirt.server.avatar_app import create_avatar_app; import uvicorn; uvicorn.run(create_avatar_app(default_backend='cuda'), host='0.0.0.0', port=int(__import__('os').environ.get('OMNIRT_PORT','9000')))"
