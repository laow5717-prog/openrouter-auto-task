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

# 等待新 run 出现（GitHub 触发有延迟）
echo ""
echo "等待 GitHub Actions 触发..."
RUN_ID=""
for i in $(seq 1 30); do
    sleep 5
    RUN_ID=$(gh run list --workflow=build.yml --limit 5 --json databaseId,headBranch,status \
        --jq ".[] | select(.headBranch == \"$TAG\") | .databaseId" 2>/dev/null | head -1)
    if [ -n "$RUN_ID" ]; then
        echo "✓ 找到构建 run: $RUN_ID"
        break
    fi
    echo "  等待中... ($((i * 5))s)"
done

if [ -z "$RUN_ID" ]; then
    echo "未找到对应的 Actions run，请手动检查 GitHub Actions"
    exit 1
fi

# 等待构建完成
echo ""
echo "等待构建完成..."
gh run watch "$RUN_ID" --exit-status

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
