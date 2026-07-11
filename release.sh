#!/bin/bash
# 一键发版：打 tag → 推送 → 等待 Actions 构建 → 下载到 release/
set -e

TAG=${1:-}
if [ -z "$TAG" ]; then
    echo "用法: ./release.sh v0.1.1"
    exit 1
fi

echo "===================================="
echo "  发布 $TAG"
echo "===================================="

# 打 tag 并推送
git tag "$TAG"
git push origin "$TAG"
echo "✓ Tag $TAG 已推送"

# 等待 Actions 构建完成
echo ""
echo "等待 GitHub Actions 构建..."
gh run watch --exit-status $(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')

# 下载到 release/
echo ""
echo "下载构建产物..."
mkdir -p release
gh release download "$TAG" --dir release/ --clobber
echo ""
echo "===================================="
echo "  完成！产物在 release/"
ls -lh release/*.zip
echo "===================================="
